"""
Upload-Post Service — Sosyal Medya Yayın API Client
=====================================================
Upload-Post (https://www.upload-post.com/?linkId=lp_144414&sourceId=dolunay&tenantId=upload-post-app) üzerinden TikTok / YouTube / Instagram /
LinkedIn / Facebook / Twitter / Threads / Pinterest gibi platformlara tek
çağrıda video yayını yapmak için sarmalayıcı.

Kontrat (canlı doğrulanmış):
- Base URL : https://api.upload-post.com/api
- Auth     : `Authorization: Apikey <JWT>`  (Bearer DEĞİL — "Apikey" keyword'ü ŞART)
- Endpoints:
    GET  /uploadposts/users          → bağlı sosyal hesaplar
    POST /upload                     → multipart upload (video veya video_url)
    GET  /uploadposts/status         → async upload polling

Tasarım:
- requests (projedeki diğer servislerle uyumlu)
- utils.retry.retry_api_call (5 deneme, exponential backoff)
- Idempotency-Key header → duplicate post guard
- Caption expansion: per-platform dict + `_override` global metin desteği
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

import requests

from logger import get_logger
from utils.retry import retry_api_call

log = get_logger("upload_post_service")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Sabitler
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DEFAULT_BASE_URL = "https://api.upload-post.com/api"
DEFAULT_PROFILE = "<UPLOAD_POST_PROFILE>"

GET_TIMEOUT = 15
POST_TIMEOUT = 60

# Upload-Post tarafından desteklenen platformlar (whitelist — typo guard)
SUPPORTED_PLATFORMS = {
    "tiktok",
    "youtube",
    "instagram",
    "linkedin",
    "facebook",
    "twitter",
    "threads",
    "pinterest",
}

# YouTube default'ları (Upload-Post API kabul eder)
YOUTUBE_DEFAULT_PRIVACY = "public"
YOUTUBE_DEFAULT_CATEGORY_ID = "22"  # People & Blogs

# TikTok default'ları
TIKTOK_DEFAULT_PRIVACY = "PUBLIC_TO_EVERYONE"

# Instagram default media type
INSTAGRAM_DEFAULT_MEDIA_TYPE = "REELS"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Exceptions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class UploadPostError(Exception):
    """Upload-Post servis hatalarının kök sınıfı."""


class UploadPostAuthError(UploadPostError):
    """401/403 — API key geçersiz veya yetkisiz."""


class UploadPostClientError(UploadPostError):
    """4xx genel istemci hatası (400, 404, 422 vb.)."""


class UploadPostServerError(UploadPostError):
    """5xx — sunucu tarafı geçici hata."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Yardımcılar
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# Platform caption / description limitleri (Upload-Post API tarafının
# downstream platformlar için zorladığı sınırlar; aşılırsa platform reddediyor).
PLATFORM_CAPTION_LIMITS = {
    "tiktok": 2200,
    "instagram": 2200,
    "youtube_description": 5000,
    "youtube_title": 100,
}


def _cap(text: str, limit: int) -> str:
    """Caption metnini platform limitine göre güvenli şekilde kısaltır.

    Limit aşılıyorsa son üç karakteri "..." yapar; aksi halde dokunmaz.
    """
    if not isinstance(text, str):
        return text
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


def _flatten_hashtags(hashtags: list[str] | None) -> str:
    """`['fashion', 'style']` → `'#fashion #style'`. None/[] → ''."""
    if not hashtags:
        return ""
    cleaned = []
    for tag in hashtags:
        if not tag:
            continue
        # '#fashion' veya 'fashion' her iki giriş formatını da kabul et
        token = str(tag).strip().lstrip("#").replace(" ", "")
        if token:
            cleaned.append(f"#{token}")
    return " ".join(cleaned)


def _compose_caption(caption: str, hashtags: list[str] | None) -> str:
    """Caption metni + hashtag stringini birleştirir (tek boşluk ayrımı)."""
    base = (caption or "").strip()
    tag_str = _flatten_hashtags(hashtags)
    if base and tag_str:
        return f"{base}\n\n{tag_str}"
    return base or tag_str


def _make_idempotency_key(video_url: str, platforms: list[str]) -> str:
    """video_url + sorted platforms + dakika-bazlı timestamp → 32 char sha256."""
    minute_bucket = int(time.time() // 60)
    raw = f"{video_url}|{','.join(sorted(platforms))}|{minute_bucket}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _raise_for_status(response: requests.Response, context: str) -> None:
    """Status code'a göre uygun UploadPostError fırlatır."""
    status = response.status_code
    if status < 400:
        return
    # Hata mesajını çıkartmaya çalış
    try:
        body = response.json()
        detail = body.get("message") or body.get("error") or body.get("detail") or str(body)
    except (ValueError, AttributeError):
        detail = response.text[:500] if response.text else ""

    msg = f"[{context}] HTTP {status}: {detail}"
    if status in (401, 403):
        raise UploadPostAuthError(msg)
    if 400 <= status < 500:
        # retry_api_call decorator'ünün retry yapabilmesi için requests.HTTPError
        # fırlatmamız gerekiyor ki status code'u görüp 429/408 vb. yeniden dener.
        # Bizim domain hatamızı bunun üstüne sarıp wrap'liyoruz: 4xx (non-retryable)
        # için doğrudan UploadPostClientError (HTTPError'dan türetip ortak iş yapacağız).
        raise UploadPostClientError(msg)
    raise UploadPostServerError(msg)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Servis Sınıfı
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class UploadPostService:
    """Upload-Post API ile video yayını ve hesap durumu kontrolü."""

    def __init__(
        self,
        api_key: str,
        profile_name: str = DEFAULT_PROFILE,
        base_url: str = DEFAULT_BASE_URL,
    ):
        if not api_key:
            raise ValueError("UploadPostService: api_key boş olamaz.")
        self.api_key = api_key
        self.profile_name = profile_name
        self.base_url = base_url.rstrip("/")
        self.auth_header = f"Apikey {self.api_key}"

    # ─────────────────────────────────────
    # Internal HTTP yardımcıları
    # ─────────────────────────────────────

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": self.auth_header}
        if extra:
            headers.update(extra)
        return headers

    # ─────────────────────────────────────
    # 1) Bağlı platform listesi
    # ─────────────────────────────────────

    @retry_api_call(max_retries=5, base_delay=2.0, operation_name="UploadPost.list_users")
    def _fetch_users_raw(self) -> dict[str, Any]:
        url = f"{self.base_url}/uploadposts/users"
        resp = requests.get(url, headers=self._headers(), timeout=GET_TIMEOUT)
        # 4xx/5xx — retry decorator'üne karar verdirebilmek için HTTPError
        if resp.status_code >= 400:
            # Auth kalıcı hata (401/403) → retry'a girmesin diye decorator'a HTTPError vermeden
            # özel exception fırlat (decorator HTTPError'u 401 için retry yapar; biz kalıcı
            # kabul edip kullanıcıya net hata dönüyoruz)
            if resp.status_code in (401, 403):
                _raise_for_status(resp, "list_users")
            resp.raise_for_status()
        return resp.json()

    def list_connected_platforms(self) -> dict[str, dict[str, Any]]:
        """
        Profile bazlı bağlı sosyal hesapları döndürür.

        Returns:
            {
              "tiktok":    {"connected": True,  "username": "meltem.2035"},
              "youtube":   {"connected": True,  "username": "@meltem.2035"},
              "instagram": {"connected": False, "username": None},
              ...
            }

        `self.profile_name`e uyan profil bulunamazsa, tüm desteklenen platformlar
        `connected=False` olarak döner.
        """
        data = self._fetch_users_raw()
        profiles = data.get("profiles") or []

        target_profile = None
        for p in profiles:
            if p.get("username") == self.profile_name:
                target_profile = p
                break

        # Default: tüm desteklenen platformlar bağlı değil
        result: dict[str, dict[str, Any]] = {
            platform: {"connected": False, "username": None}
            for platform in SUPPORTED_PLATFORMS
        }

        if not target_profile:
            log.warning(
                f"Upload-Post: profil '{self.profile_name}' bulunamadi "
                f"({len(profiles)} profil mevcut)."
            )
            return result

        social_accounts = target_profile.get("social_accounts") or {}
        for platform, account in social_accounts.items():
            # API'nin döndürdüğü gerçek tipler:
            #   bağlı  → dict (handle, display_name, ...)
            #   bağsız → "" (boş string) veya None
            if isinstance(account, dict) and account:
                username = (
                    account.get("handle")
                    or account.get("username")
                    or account.get("display_name")
                )
                # Bazı platformlarda reauth gerekebilir; yine de "connected" kabul et,
                # ama bayrağı dışa açık tut.
                entry = {
                    "connected": True,
                    "username": username,
                }
                if "reauth_required" in account:
                    entry["reauth_required"] = bool(account["reauth_required"])
                result[platform] = entry
            else:
                # Bağsız ya da bilinmeyen format
                result[platform] = {"connected": False, "username": None}

        return result

    # ─────────────────────────────────────
    # 2) Video yayını
    # ─────────────────────────────────────

    @retry_api_call(max_retries=5, base_delay=2.0, operation_name="UploadPost.upload_video")
    def upload_video(
        self,
        video_url: str,
        platforms: list[str],
        captions: dict[str, dict[str, Any]],
        async_upload: bool = True,
    ) -> dict[str, Any]:
        """
        Çoklu-platform video yayını başlatır.

        Args:
            video_url: Public erişilebilir video URL (örn. Replicate CDN).
            platforms: ["tiktok", "youtube", "instagram", ...].
            captions: Per-platform metin/hashtag/tag dict'i.
                {
                  "tiktok":    {"caption": "...", "hashtags": ["a", "b"]},
                  "youtube":   {"title": "...", "description": "...",
                                "tags": ["a", "b"]},
                  "instagram": {"caption": "...", "hashtags": [...]},
                  "_override": "Tüm platformlara aynı metin (caption + hashtag birleşik)",
                }
            async_upload: True → request_id döner; False → senkron sonuç.

        Returns:
            API response dict (request_id, platforms, vs.).
        """
        if not video_url:
            raise ValueError("upload_video: video_url boş olamaz.")
        if not platforms:
            raise ValueError("upload_video: en az bir platform gerekli.")

        # Platform whitelist kontrolü (early fail)
        invalid = [p for p in platforms if p not in SUPPORTED_PLATFORMS]
        if invalid:
            raise ValueError(
                f"upload_video: desteklenmeyen platform(lar): {invalid}. "
                f"Geçerli liste: {sorted(SUPPORTED_PLATFORMS)}"
            )

        form_data = self._build_form_data(video_url, platforms, captions, async_upload)

        idem_key = _make_idempotency_key(video_url, platforms)
        headers = self._headers({"Idempotency-Key": idem_key})

        url = f"{self.base_url}/upload"
        log.info(
            f"Upload-Post upload basliyor: platforms={platforms} async={async_upload} "
            f"idem={idem_key[:8]}..."
        )

        # requests multipart: list-of-tuples ile aynı key birden çok değer (platform[])
        resp = requests.post(
            url,
            headers=headers,
            data=form_data,
            timeout=POST_TIMEOUT,
        )

        if resp.status_code >= 400:
            # 401/403 → kalıcı auth hatası (UploadPostAuthError)
            if resp.status_code in (401, 403):
                _raise_for_status(resp, "upload_video")
            # 408/429/5xx → retry decorator'ün karar vermesi için HTTPError raise
            if resp.status_code in (408, 429) or resp.status_code >= 500:
                resp.raise_for_status()
            # Diğer 4xx (400/404/422/...) → kalıcı istemci hatası;
            # response body'sini UploadPostClientError ile kullanıcıya yansıt.
            _raise_for_status(resp, "upload_video")

        try:
            payload = resp.json()
        except ValueError:
            raise UploadPostError(
                f"upload_video: JSON parse edilemedi (HTTP {resp.status_code}): "
                f"{resp.text[:300]}"
            )

        log.info(
            f"Upload-Post upload OK: request_id={payload.get('request_id')} "
            f"keys={list(payload.keys())}"
        )
        return payload

    def _build_form_data(
        self,
        video_url: str,
        platforms: list[str],
        captions: dict[str, dict[str, Any]],
        async_upload: bool,
    ) -> list[tuple[str, str]]:
        """
        Caption dict'inden Upload-Post multipart form alanlarını üretir.

        Çoklu değerli alanlar (örn. `platform[]`) için requests'in kabul ettiği
        list-of-tuples formatı kullanılır.
        """
        form: list[tuple[str, str]] = []

        # Sabit alanlar
        # Upload-Post POST /upload form alan adı: `video` (URL veya binary).
        # 2026-05-09 canlı doğrulandı: `video_url` reddediliyor (HTTP 400
        # "Video file or video url is required"); `video` ile URL kabul ediliyor.
        form.append(("video", video_url))
        form.append(("user", self.profile_name))
        form.append(("async_upload", "true" if async_upload else "false"))

        # Platform listesi (her biri ayrı satır — Upload-Post `platform[]` dizisi)
        for p in platforms:
            form.append(("platform[]", p))

        # Override mekanizması: tüm seçili platformlara aynı metin
        override_text = captions.get("_override") if isinstance(captions, dict) else None
        if isinstance(override_text, str) and override_text.strip():
            override = override_text.strip()
            # Title: ilk 90 karakter (YouTube title hard cap 100)
            title = _cap(override[:90], PLATFORM_CAPTION_LIMITS["youtube_title"])
            form.append(("title", title))
            # Description ortak metin; YouTube description max 5000.
            form.append(("description", _cap(override, PLATFORM_CAPTION_LIMITS["youtube_description"])))
            # Platform-spesifik default'ları yine ekle (tags vb. yok)
            self._append_platform_defaults(form, platforms, captions, override_text=override)
            return form

        # Per-platform mod
        # Title belirleme: önce youtube["title"], yoksa ilk platformun caption[:90]
        title = _cap(self._derive_title(platforms, captions), PLATFORM_CAPTION_LIMITS["youtube_title"])
        form.append(("title", title))

        # Description = ilk platformun caption + hashtag birleştirilmiş metni
        description = _cap(
            self._derive_description(platforms, captions),
            PLATFORM_CAPTION_LIMITS["youtube_description"],
        )
        form.append(("description", description))

        # Platform-spesifik alanlar
        self._append_platform_defaults(form, platforms, captions)

        return form

    def _derive_title(
        self,
        platforms: list[str],
        captions: dict[str, dict[str, Any]],
    ) -> str:
        # YouTube'un title'ı en spesifik olanı; varsa onu kullan
        yt = captions.get("youtube") or {}
        if isinstance(yt, dict) and yt.get("title"):
            return str(yt["title"])[:200]

        # Yoksa ilk platformun caption'ından ilk 90 karakter
        for p in platforms:
            spec = captions.get(p) or {}
            if isinstance(spec, dict):
                cap = spec.get("caption") or spec.get("title")
                if cap:
                    return str(cap)[:90]
        return "Video"

    def _derive_description(
        self,
        platforms: list[str],
        captions: dict[str, dict[str, Any]],
    ) -> str:
        # YouTube description öncelikli (uzun-form)
        yt = captions.get("youtube") or {}
        if isinstance(yt, dict) and yt.get("description"):
            return str(yt["description"])

        # Yoksa ilk platformun caption + hashtag
        for p in platforms:
            spec = captions.get(p) or {}
            if isinstance(spec, dict):
                composed = _compose_caption(
                    spec.get("caption", ""),
                    spec.get("hashtags"),
                )
                if composed:
                    return composed
        return ""

    def _append_platform_defaults(
        self,
        form: list[tuple[str, str]],
        platforms: list[str],
        captions: dict[str, dict[str, Any]],
        override_text: str | None = None,
    ) -> None:
        """Her platform için Upload-Post'un beklediği özel alanları ekler."""

        if "youtube" in platforms:
            yt = captions.get("youtube") or {}
            tags = yt.get("tags") if isinstance(yt, dict) else None
            if tags:
                form.append(
                    ("youtube_tags", ",".join(str(t).lstrip("#") for t in tags))
                )
            form.append(("youtube_categoryId", str(yt.get("categoryId", YOUTUBE_DEFAULT_CATEGORY_ID))))
            form.append(("youtube_privacyStatus", str(yt.get("privacyStatus", YOUTUBE_DEFAULT_PRIVACY))))

        if "tiktok" in platforms:
            tk = captions.get("tiktok") or {}
            form.append(
                ("tiktok_privacy_level", str(tk.get("privacy_level", TIKTOK_DEFAULT_PRIVACY)))
            )
            # TikTok caption-spesifik (override yoksa)
            if override_text is None and isinstance(tk, dict):
                composed = _compose_caption(tk.get("caption", ""), tk.get("hashtags"))
                if composed:
                    form.append(
                        ("tiktok_description", _cap(composed, PLATFORM_CAPTION_LIMITS["tiktok"]))
                    )
            elif override_text is not None:
                form.append(
                    ("tiktok_description", _cap(override_text, PLATFORM_CAPTION_LIMITS["tiktok"]))
                )

        if "instagram" in platforms:
            ig = captions.get("instagram") or {}
            form.append(
                ("instagram_media_type", str(ig.get("media_type", INSTAGRAM_DEFAULT_MEDIA_TYPE)))
            )
            if override_text is None and isinstance(ig, dict):
                composed = _compose_caption(ig.get("caption", ""), ig.get("hashtags"))
                if composed:
                    form.append(
                        ("instagram_caption", _cap(composed, PLATFORM_CAPTION_LIMITS["instagram"]))
                    )
            elif override_text is not None:
                form.append(
                    ("instagram_caption", _cap(override_text, PLATFORM_CAPTION_LIMITS["instagram"]))
                )

        # LinkedIn / Facebook / Twitter / Threads / Pinterest:
        # Upload-Post bunlar için description alanını kullanıyor; ek bir form
        # alanı zorunlu değil. Override durumunda description zaten ortak metin.

    # ─────────────────────────────────────
    # 3) Status polling
    # ─────────────────────────────────────

    @retry_api_call(max_retries=5, base_delay=2.0, operation_name="UploadPost.status")
    def _fetch_status_raw(self, request_id: str) -> dict[str, Any]:
        url = f"{self.base_url}/uploadposts/status"
        resp = requests.get(
            url,
            headers=self._headers(),
            params={"request_id": request_id},
            timeout=GET_TIMEOUT,
        )
        if resp.status_code >= 400:
            if resp.status_code in (401, 403):
                _raise_for_status(resp, "status")
            resp.raise_for_status()
        return resp.json()

    def poll_status(
        self,
        request_id: str,
        timeout_s: int = 120,
        interval_s: int = 5,
    ) -> dict[str, Any]:
        """
        Async upload status'unu poll eder, sonuç (completed/failed) ya da timeout
        durana kadar bekler.

        Returns:
            {
              "status": "completed" | "processing" | "failed" | "timeout",
              "results": { "tiktok": {"post_url": "...", "success": True, ...}, ... },
              "errors":  { "instagram": "error message", ... },
              "raw": <son API response>,
            }

        NOT: Upload-Post API `results` alanını LİSTE olarak döner
        (`[{"platform": "tiktok", "post_url": "...", "success": True}, ...]`);
        biz burada platform-key dict'e normalize ediyoruz ki tüketici (main.py)
        `for platform, info in results.items()` ile direkt iterate edebilsin.
        """
        if not request_id:
            raise ValueError("poll_status: request_id boş olamaz.")

        deadline = time.time() + max(1, timeout_s)
        last: dict[str, Any] = {}

        while time.time() < deadline:
            payload = self._fetch_status_raw(request_id)
            last = payload
            status = (payload.get("status") or "").lower()

            if status in ("completed", "success", "succeeded"):
                results, errors = self._normalize_results(payload)
                return {
                    "status": "completed",
                    "results": results,
                    "errors": errors,
                    "raw": payload,
                }
            if status in ("failed", "error"):
                results, errors = self._normalize_results(payload)
                if not errors:
                    errors = {"_global": payload.get("message") or "Upload failed"}
                return {
                    "status": "failed",
                    "results": results,
                    "errors": errors,
                    "raw": payload,
                }

            time.sleep(max(1, interval_s))

        results, errors = self._normalize_results(last)
        return {
            "status": "timeout",
            "results": results,
            "errors": errors,
            "raw": last,
        }

    @staticmethod
    def _normalize_results(
        payload: dict[str, Any],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        """
        API response'unun `results` (list veya dict) ve `errors` alanlarını
        platform-key normal formata çevirir.

        Per-platform `success: false` durumunda result yine results map'ine
        konur (post_url None) ve `errors[platform]` = error_message yazılır.
        """
        raw_results = payload.get("results") or payload.get("platforms") or {}
        raw_errors = payload.get("errors") or {}

        results_map: dict[str, dict[str, Any]] = {}
        errors_map: dict[str, str] = {}

        if isinstance(raw_results, dict):
            for platform, info in raw_results.items():
                if not isinstance(info, dict):
                    continue
                results_map[platform] = info
                if info.get("success") is False:
                    errors_map[platform] = (
                        info.get("error_message") or info.get("error") or "Upload failed"
                    )
        elif isinstance(raw_results, list):
            for item in raw_results:
                if not isinstance(item, dict):
                    continue
                platform = item.get("platform")
                if not platform:
                    continue
                # main.py kolay tüketim için url alias
                normalized = dict(item)
                if "post_url" in normalized and "url" not in normalized:
                    normalized["url"] = normalized["post_url"]
                results_map[platform] = normalized
                if item.get("success") is False:
                    errors_map[platform] = (
                        item.get("error_message") or item.get("error") or "Upload failed"
                    )
        else:
            # Beklenmeyen format (string, None, int vb.). Sessizce boş dönmek
            # publishing flow'da "her şey OK ama hiçbir platforma yüklenmedi"
            # tarzı sahte başarıya yol açar — explicit hata fırlat.
            raise ValueError(
                "Upload-Post yanıtı beklenmeyen formatta "
                f"(tip: {type(raw_results).__name__}); platforma yüklenemedi"
            )

        if isinstance(raw_errors, dict):
            for platform, msg in raw_errors.items():
                if platform not in errors_map and msg:
                    errors_map[platform] = str(msg)

        return results_map, errors_map
