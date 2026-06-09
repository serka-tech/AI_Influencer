from __future__ import annotations

"""
Kie AI Service — Nano Banana Pro (görsel) + Veo 3.1 (video)
============================================================
AI Influencer pipeline için Kie AI istemcisi.

İki üretim modeli:
- Görsel: `nano-banana-pro` → /jobs/createTask + /jobs/recordInfo (state bazlı poll)
- Video : `veo3_fast`       → /veo/generate   + /veo/record-info (successFlag bazlı poll)

NOT: Veo, standart createTask yerine kendine ait endpoint kullanır ve payload düz (flat)
JSON'dır (`input` objesine sarılmaz). Bu, n8n "SKOOL - Çoklu Sahne Veo 3.1" akışındaki
çalışan kurulumla birebir aynıdır.
"""

import json
import os
import re
import time

import requests

from logger import get_logger
from utils.retry import retry_api_call

log = get_logger("kie_api")

# Polling ayarları (görsel — createTask/recordInfo)
POLL_INTERVAL_SECONDS = 10
MAX_POLL_ATTEMPTS = 90  # ~15 dakika tampon
ASYNC_POLL_HARD_TIMEOUT = 600
REQUEST_TIMEOUT = 30

# Veo polling — video üretimi daha uzun sürer
VEO_MAX_POLL_ATTEMPTS = 120          # ~20 dakika tampon
VEO_ASYNC_POLL_HARD_TIMEOUT = 900    # 15 dakika hard cap

# File Upload
FILE_UPLOAD_BASE_URL = "https://kieai.redpandaai.co"

VALID_ASPECT_RATIOS = {"9:16", "16:9", "1:1", "4:3", "3:4", "21:9"}
DEFAULT_ASPECT_RATIO = "9:16"


def normalize_aspect_ratio(raw: str) -> str:
    """Ham aspect_ratio string'ini Kie AI'ın kabul ettiği formata dönüştürür."""
    if not raw:
        return DEFAULT_ASPECT_RATIO

    cleaned = str(raw).strip().lower()
    if cleaned in VALID_ASPECT_RATIOS:
        return cleaned

    ratio_match = re.search(r'(\d+)\s*[:x/]\s*(\d+)', cleaned)
    if ratio_match:
        candidate = f"{ratio_match.group(1)}:{ratio_match.group(2)}"
        if candidate in VALID_ASPECT_RATIOS:
            return candidate

    label_map = {
        "dikey": "9:16", "vertical": "9:16", "portrait": "9:16",
        "yatay": "16:9", "horizontal": "16:9", "landscape": "16:9", "widescreen": "16:9",
        "kare": "1:1", "square": "1:1",
        "ultrawide": "21:9", "cinematic": "21:9",
    }
    for keyword, ratio in label_map.items():
        if keyword in cleaned:
            return ratio

    log.warning(
        f"Bilinmeyen aspect_ratio normalize edildi: '{raw}' → '{DEFAULT_ASPECT_RATIO}'"
    )
    return DEFAULT_ASPECT_RATIO


def _is_kie_native_url(url: str) -> bool:
    """URL'nin Kie AI sunucularında olup olmadığını kontrol eder (domain-level)."""
    if not url:
        return False
    try:
        from urllib.parse import urlparse
        hostname = urlparse(url).hostname or ""
        native_domains = ("kieai.redpandaai.co", "templateb.aiquickdraw.com", "kie.ai")
        return any(hostname == d or hostname.endswith(f".{d}") for d in native_domains)
    except Exception:
        return False


def _extract_urls_from_veo_data(data: dict) -> list[str]:
    """
    Veo record-info `data` objesinden video URL'lerini çıkarır.

    Kie API sürümler arası farklı yerlere koyabiliyor; sırayla dene:
    - data.response.resultUrls (n8n akışındaki konum)
    - data.resultUrls
    - data.resultJson (string JSON) → resultUrls / resultUrl / url
    - data.video_url / data.response.video_url
    """
    if not isinstance(data, dict):
        return []

    resp = data.get("response") or {}
    if isinstance(resp, dict):
        urls = resp.get("resultUrls")
        if isinstance(urls, list) and urls:
            return [u for u in urls if isinstance(u, str) and u.startswith("http")]
        if isinstance(resp.get("video_url"), str) and resp["video_url"].startswith("http"):
            return [resp["video_url"]]

    urls = data.get("resultUrls")
    if isinstance(urls, list) and urls:
        return [u for u in urls if isinstance(u, str) and u.startswith("http")]

    raw = data.get("resultJson")
    if raw:
        try:
            obj = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(obj, dict):
                ru = obj.get("resultUrls")
                if isinstance(ru, list) and ru:
                    return [u for u in ru if isinstance(u, str) and u.startswith("http")]
                for key in ("resultUrl", "url", "video_url"):
                    if isinstance(obj.get(key), str) and obj[key].startswith("http"):
                        return [obj[key]]
        except Exception:
            m = re.search(r'https?://[^\s"\']+', str(raw))
            if m:
                return [m.group(0)]

    if isinstance(data.get("video_url"), str) and data["video_url"].startswith("http"):
        return [data["video_url"]]

    return []


class KieAIService:
    """Kie AI API ile görsel (Nano Banana Pro) ve video (Veo 3.1) üretimi."""

    def __init__(self, api_key: str, base_url: str = "https://api.kie.ai/api/v1/"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 🖼️ GÖRSEL ÜRETİMİ — Nano Banana Pro (image-to-image)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def create_image(
        self,
        prompt: str,
        image_input: list[str] | None = None,
        aspect_ratio: str = "9:16",
        resolution: str = "1K",
        output_format: str = "png",
        model: str = "nano-banana-pro",
    ) -> str:
        """
        Nano Banana Pro ile görsel üretim görevi oluşturur (taskId döner).

        Args:
            prompt: Görsel açıklaması (İngilizce önerilir)
            image_input: Referans görsel URL listesi (karakter referansı)
            aspect_ratio: "9:16" vb.
            resolution: "1K" / "2K" / "4K"
            output_format: "png" / "jpeg"
            model: Kie model adı (varsayılan nano-banana-pro)
        """
        safe_aspect = normalize_aspect_ratio(aspect_ratio)

        input_data = {
            "prompt": prompt,
            "aspect_ratio": safe_aspect,
            "resolution": resolution,
            "output_format": output_format,
        }
        if image_input:
            input_data["image_input"] = image_input

        payload = {"model": model, "input": input_data}
        task_id = self._create_task(payload)
        log.info(f"{model} görsel görevi oluşturuldu: {task_id} ({safe_aspect}, {resolution})")
        return task_id

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 🎬 VİDEO ÜRETİMİ — Veo 3.1 (image-to-video)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def create_veo_video(
        self,
        prompt: str,
        image_url: str,
        aspect_ratio: str = "9:16",
        model: str = "veo3_fast",
        generation_type: str = "FIRST_AND_LAST_FRAMES_2_VIDEO",
        enable_translation: bool = True,
        enable_fallback: bool = False,
    ) -> str:
        """
        Veo 3.1 ile image-to-video görevi oluşturur (taskId döner).

        n8n "Veo 3.1 Image to Video" node ile birebir payload.
        Endpoint: POST /veo/generate (düz JSON, `input` sarmalı YOK).

        Args:
            prompt: image_to_video_prompt (İngilizce, ses + hareket tarifi)
            image_url: İlk kare görseli (Nano Banana Pro çıktısı)
            aspect_ratio: "9:16"
            model: "veo3_fast"
            generation_type: "FIRST_AND_LAST_FRAMES_2_VIDEO"
        """
        safe_aspect = normalize_aspect_ratio(aspect_ratio)
        payload = {
            "prompt": prompt,
            "imageUrls": [image_url],
            "model": model,
            "aspectRatio": safe_aspect,
            "enableFallback": enable_fallback,
            "enableTranslation": enable_translation,
            "generationType": generation_type,
        }
        task_id = self._create_veo_task(payload)
        log.info(f"Veo video görevi oluşturuldu: {task_id} ({model}, {safe_aspect})")
        return task_id

    @retry_api_call(max_retries=2, base_delay=2.0, operation_name="Kie AI veo/generate")
    def _create_veo_task(self, payload: dict) -> str:
        url = f"{self.base_url}/veo/generate"
        response = requests.post(url, headers=self.headers, json=payload, timeout=REQUEST_TIMEOUT)

        if response.status_code == 422:
            log.error(
                f"Veo 422 Validation Error — "
                f"payload={json.dumps(payload, ensure_ascii=False)[:400]}, "
                f"response={response.text[:400]}"
            )
        if response.status_code >= 500:
            log.error(f"Veo upstream hatası: HTTP {response.status_code} — {response.text[:400]}")

        response.raise_for_status()
        data = response.json()

        code = data.get("code")
        if code is not None and str(code) not in ("200", "0"):
            error_msg = data.get("msg", "Bilinmeyen hata")
            code_int = int(code) if str(code).isdigit() else 400
            if code_int in {401, 408, 429} or (500 <= code_int <= 599):
                response.status_code = code_int
                raise requests.exceptions.HTTPError(
                    f"Veo generate API hatası: {error_msg} (code={code})", response=response
                )
            raise ValueError(f"Veo generate hatası: {error_msg} (code={code})")

        task_id = data.get("data", {}).get("taskId") or data.get("taskId")
        if not task_id:
            raise ValueError(f"Veo generate yanıtında taskId yok: {data}")
        return task_id

    def poll_veo_task(self, task_id: str, callback=None) -> dict:
        """
        Veo görevini tamamlanana kadar poll eder (sync).

        successFlag mantığı (n8n "Video Kontrol"):
            0 → üretiliyor, 1 → başarılı, 2 → task failed, 3 → generation failed

        Returns:
            {"status": "success", "urls": [...]} veya {"status": "failed", "error": "...", "code": "..."}
        """
        url = f"{self.base_url}/veo/record-info"
        prev_flag = None

        for attempt in range(1, VEO_MAX_POLL_ATTEMPTS + 1):
            try:
                response = requests.get(
                    url, params={"taskId": task_id}, headers=self.headers, timeout=REQUEST_TIMEOUT
                )
                if response.status_code in (401, 403, 404):
                    raise RuntimeError(
                        f"permanent: HTTP {response.status_code} veo poll — {response.text[:200]}"
                    )
                response.raise_for_status()
                data = response.json()

                code = data.get("code")
                if code is not None and str(code) not in ("200", "0"):
                    code_int = int(code) if str(code).isdigit() else 0
                    if code_int in (401, 403, 404):
                        raise RuntimeError(f"permanent: wrapper code={code}")
                    raise ValueError(f"Veo poll wrapper hatası (code={code}): {data.get('msg')}")

                d = data.get("data", {}) or {}
                flag = d.get("successFlag")

                if callback:
                    callback(attempt, flag)

                if flag == 1:
                    urls = _extract_urls_from_veo_data(d)
                    if not urls:
                        log.error(f"Veo successFlag=1 ama URL bulunamadı: {task_id} — {d}")
                        return {"status": "failed", "error": "Video URL bulunamadı"}
                    log.info(f"Veo görevi tamamlandı: {task_id} — {len(urls)} video, {attempt} deneme")
                    return {"status": "success", "urls": urls}

                if flag in (2, 3):
                    fail_msg = d.get("failMsg", "Bilinmeyen hata")
                    fail_code = d.get("failCode", "")
                    label = "task failed" if flag == 2 else "generation failed"
                    log.error(f"Veo {label}: {task_id} — code={fail_code} msg={fail_msg}")
                    return {"status": "failed", "error": fail_msg, "code": str(fail_code)}

                if flag != prev_flag:
                    log.info(f"Veo poll {task_id}: flag {prev_flag}→{flag} ({attempt}/{VEO_MAX_POLL_ATTEMPTS})")
                    prev_flag = flag

            except RuntimeError as e:
                if str(e).startswith("permanent:"):
                    log.error(f"Veo poll kalıcı hata: {task_id} — {e}")
                    raise
                log.error(f"Veo poll hatası ({attempt}): {task_id}", exc_info=True)
            except Exception:
                log.error(f"Veo poll hatası ({attempt}): {task_id}", exc_info=True)

            time.sleep(10)

        log.error(f"Veo poll timeout: {task_id}")
        return {"status": "failed", "error": "Veo polling timeout — görev süre aşımına uğradı"}

    async def async_poll_veo_task(self, task_id: str, callback=None) -> dict:
        """poll_veo_task'ın async sürümü — event loop'u bloke etmez."""
        import asyncio as _asyncio
        try:
            return await _asyncio.wait_for(
                _asyncio.to_thread(self.poll_veo_task, task_id, callback),
                timeout=VEO_ASYNC_POLL_HARD_TIMEOUT,
            )
        except _asyncio.TimeoutError:
            log.error(f"Veo async poll hard timeout: {task_id}")
            return {"status": "failed", "error": "Veo polling hard timeout"}

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 🔁 GÖRSEL POLLING — createTask state modeli
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def poll_task(self, task_id: str, callback=None) -> dict:
        """Görsel görevini tamamlanana kadar poll eder (state bazlı)."""
        url = f"{self.base_url}/jobs/recordInfo"
        start_time = time.monotonic()
        prev_state: str | None = None

        for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
            try:
                response = requests.get(
                    url, params={"taskId": task_id}, headers=self.headers, timeout=REQUEST_TIMEOUT
                )
                if response.status_code in (401, 403, 404):
                    raise RuntimeError(
                        f"permanent: HTTP {response.status_code} polling — {response.text[:200]}"
                    )
                response.raise_for_status()
                data = response.json()

                code = data.get("code")
                if code is not None and str(code) not in ("200", "0"):
                    code_int = int(code) if str(code).isdigit() else 0
                    if code_int in (401, 403, 404):
                        raise RuntimeError(f"permanent: wrapper code={code}")
                    raise ValueError(f"Polling wrapper hatası (code={code}): {data.get('msg')}")

                state = data.get("data", {}).get("state", "unknown")
                if callback:
                    callback(attempt, state)

                if state == "success":
                    result_json = data["data"].get("resultJson", "{}")
                    parsed = json.loads(result_json) if isinstance(result_json, str) else result_json
                    urls = parsed.get("resultUrls", [])
                    log.info(f"Görsel görevi tamamlandı: {task_id} — {len(urls)} çıktı, {attempt} deneme")
                    return {"status": "success", "urls": urls}

                if state in ("failed", "fail"):
                    fail_msg = data["data"].get("failMsg", "Bilinmeyen hata")
                    fail_code = data["data"].get("failCode", "")
                    log.error(f"Görsel görevi başarısız: {task_id} — {fail_msg}")
                    return {"status": "failed", "error": fail_msg, "code": str(fail_code)}

                if state != prev_state:
                    log.info(f"Polling {task_id}: state {prev_state}→{state} ({attempt}/{MAX_POLL_ATTEMPTS})")
                    prev_state = state

            except RuntimeError as e:
                if str(e).startswith("permanent:"):
                    log.error(f"Polling kalıcı hata: {task_id} — {e}")
                    raise
                log.error(f"Polling hatası ({attempt}): {task_id}", exc_info=True)
            except Exception:
                log.error(f"Polling hatası ({attempt}): {task_id}", exc_info=True)

            elapsed = time.monotonic() - start_time
            time.sleep(20 if elapsed < 30 else 8)

        log.error(f"Polling timeout: {task_id}")
        return {"status": "failed", "error": "Polling timeout — görev süre aşımına uğradı"}

    async def async_poll_task(self, task_id: str, callback=None) -> dict:
        """poll_task'ın async sürümü."""
        import asyncio as _asyncio
        try:
            return await _asyncio.wait_for(
                _asyncio.to_thread(self.poll_task, task_id, callback),
                timeout=ASYNC_POLL_HARD_TIMEOUT,
            )
        except _asyncio.TimeoutError:
            log.error(f"Async poll hard timeout: {task_id}")
            return {"status": "failed", "error": "Polling hard timeout"}

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 💰 KREDİ
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def get_credit_balance(self) -> dict:
        """Kie AI hesap bakiyesini sorgular."""
        try:
            url = f"{self.base_url}/chat/credit"
            response = requests.get(url, headers=self.headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            log.info(f"Kredi bakiyesi sorgulandı: {data}")
            return data
        except Exception:
            log.error("Kredi sorgulama hatası", exc_info=True)
            return {}

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 📤 DOSYA YÜKLEME
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @retry_api_call(max_retries=2, base_delay=2.0, operation_name="Kie AI file upload")
    def upload_file_from_url(
        self,
        file_url: str,
        file_name: str | None = None,
        upload_path: str = "images/user-uploads",
    ) -> str:
        """Harici URL'deki dosyayı Kie AI dosya sunucusuna yükler, downloadUrl döner."""
        if not file_name:
            from urllib.parse import urlparse
            parsed = urlparse(file_url)
            file_name = os.path.basename(parsed.path) or "uploaded_file"

        url = f"{FILE_UPLOAD_BASE_URL}/api/file-url-upload"
        payload = {"fileUrl": file_url, "fileName": file_name, "uploadPath": upload_path}

        response = requests.post(url, headers=self.headers, json=payload, timeout=60)
        response.raise_for_status()
        data = response.json()

        code = data.get("code")
        if code is not None and str(code) not in ("200", "0"):
            code_int = int(code) if str(code).isdigit() else 400
            if code_int in {401, 408, 429} or (500 <= code_int <= 599):
                response.status_code = code_int
                raise requests.exceptions.HTTPError(
                    f"Upload API hatası: {data.get('msg')} (code={code})", response=response
                )
            raise ValueError(f"Kie file upload hatası: {data.get('msg')} (code={code})")

        download_url = data.get("downloadUrl") or data.get("data", {}).get("downloadUrl")
        if not download_url:
            raise ValueError(f"File upload yanıtında downloadUrl yok: {data}")
        log.info(f"Dosya yüklendi: {file_name} → {download_url[:80]}...")
        return download_url

    def upload_files_from_urls(self, file_urls: list[str]) -> list[str]:
        """Birden fazla harici URL'yi Kie AI'a toplu yükler."""
        download_urls = []
        for i, url in enumerate(file_urls, 1):
            try:
                download_urls.append(self.upload_file_from_url(url))
                log.info(f"Toplu yükleme [{i}/{len(file_urls)}]: başarılı")
            except Exception:
                log.error(f"Toplu yükleme [{i}/{len(file_urls)}] başarısız: {url}", exc_info=True)
                continue
        return download_urls

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 🔧 INTERNAL — createTask
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @retry_api_call(max_retries=2, base_delay=2.0, operation_name="Kie AI createTask")
    def _create_task(self, payload: dict) -> str:
        url = f"{self.base_url}/jobs/createTask"
        input_block = payload.get("input", {})
        if "aspect_ratio" in input_block:
            validated = normalize_aspect_ratio(input_block["aspect_ratio"])
            if validated != input_block["aspect_ratio"]:
                input_block["aspect_ratio"] = validated

        response = requests.post(url, headers=self.headers, json=payload, timeout=REQUEST_TIMEOUT)

        if response.status_code == 422:
            log.error(
                f"Kie 422 Validation Error — "
                f"input={json.dumps(input_block, ensure_ascii=False)[:400]}, "
                f"response={response.text[:400]}"
            )
        if response.status_code >= 500:
            log.error(f"Kie upstream hatası: HTTP {response.status_code} — {response.text[:400]}")

        response.raise_for_status()
        data = response.json()

        code = data.get("code")
        if code is not None and str(code) not in ("200", "0"):
            error_msg = data.get("msg", "Bilinmeyen hata")
            code_int = int(code) if str(code).isdigit() else 400
            if code_int in {401, 408, 429} or (500 <= code_int <= 599):
                response.status_code = code_int
                raise requests.exceptions.HTTPError(
                    f"Kie createTask API hatası: {error_msg} (code={code})", response=response
                )
            raise ValueError(f"Kie createTask hatası: {error_msg} (code={code})")

        task_id = data.get("data", {}).get("taskId")
        if not task_id:
            raise ValueError(f"Kie createTask yanıtında taskId yok: {data}")
        return task_id
