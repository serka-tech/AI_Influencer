from __future__ import annotations

"""
Topic Resolver — Video Konusunu Belirler
==========================================
Kullanıcının tek girişinden video konusunu üretir. Üç mod:

1. LINK    : Mekan/ürün sayfası URL'i → sayfa metni çekilir, LLM ile tanıtım konusu çıkarılır.
2. MANUEL  : Düz metin (link değil, 'yok' değil) → doğrudan konu olarak kullanılır.
3. OTOMATİK: Boş veya 'yok' → karakterin kanalına uygun ilgi çekici bir konu LLM ile üretilir.

Harici scraping servisi (Firecrawl/Tavily) gerektirmez; link okuma hafif bir HTTP fetch
ile yapılır. Sayfa okunamazsa (JS-ağır site, Instagram, harita vb.) URL + LLM çıkarımıyla
makul bir konuya düşülür ve kullanıcıya not iletilir.
"""

import re

import requests

from logger import get_logger

log = get_logger("topic_resolver")

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_NEGATIVE = {"yok", "hayır", "hayir", "boş", "bos", "-", "yo", "none", "no"}

_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "tr,en;q=0.8",
}


def is_url(text: str) -> bool:
    return bool(_URL_RE.match(text.strip()))


def is_negative(text: str) -> bool:
    return text.strip().lower() in _NEGATIVE or not text.strip()


def _strip_html(html: str) -> str:
    """Basit HTML → düz metin (bs4 bağımlılığı olmadan)."""
    html = re.sub(r"(?is)<(script|style|noscript|head)[^>]*>.*?</\1>", " ", html)
    # title ve meta description'ı öne al
    title = ""
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    if m:
        title = m.group(1)
    desc = ""
    m = re.search(r'(?is)<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', html)
    if m:
        desc = m.group(1)
    body = re.sub(r"(?is)<[^>]+>", " ", html)
    body = re.sub(r"\s+", " ", body).strip()
    combined = " ".join(p for p in [title, desc, body] if p)
    return combined.strip()


def _fetch_url_text(url: str, max_chars: int = 4000) -> str:
    """URL'den okunabilir metni çeker (best-effort). Hata olursa boş döner."""
    try:
        resp = requests.get(url, headers=_FETCH_HEADERS, timeout=20)
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "")
        if "html" not in ctype and "text" not in ctype:
            return ""
        text = _strip_html(resp.text)
        return text[:max_chars]
    except Exception:
        log.warning(f"URL okunamadı: {url[:80]}", exc_info=True)
        return ""


class TopicResolver:
    def __init__(self, openai_service):
        self.openai = openai_service

    def resolve(self, raw_input: str, character_details: str, language: str) -> tuple[str, str]:
        """
        Returns:
            (video_topic, source_note)
            video_topic : senaryo agent'ına verilecek konu metni
            source_note : kullanıcıya gösterilecek kısa bilgi ("otomatik bulundu" vb.)
        """
        raw = (raw_input or "").strip()

        if is_negative(raw):
            topic = self._auto_topic(character_details, language)
            return topic, f"🤖 Konuyu Melisa kendi buldu:\n“{topic}”"

        if is_url(raw):
            return self._from_url(raw, character_details, language)

        # Manuel metin — doğrudan konu
        return raw, ""

    # ── Otomatik konu üretimi ──
    def _auto_topic(self, character_details: str, language: str) -> str:
        prompt = (
            "Sen bir sosyal medya içerik fikri üreticisisin. Aşağıdaki influencer karakteri için "
            "TEK bir kısa, ilgi çekici, kaydırmayı durduran video konusu öner. Konu, karakterin "
            "kanalına ve şehrine uygun olsun. SADECE konu cümlesini yaz, açıklama veya tırnak ekleme. "
            "En fazla 15 kelime.\n\n"
            f"Karakter:\n{character_details}\n\n"
            f"Video dili: {language}"
        )
        topic = self.openai.chat(
            [{"role": "user", "content": prompt}], max_tokens=120
        ).strip().strip('"').strip("“”").splitlines()[0]
        log.info(f"Otomatik konu üretildi: {topic}")
        return topic

    # ── Link'ten konu çıkarımı ──
    def _from_url(self, url: str, character_details: str, language: str) -> tuple[str, str]:
        page_text = _fetch_url_text(url)

        if len(page_text) >= 200:
            prompt = (
                "Aşağıda bir mekan/ürün/işletme web sayfasından çekilmiş metin var. Bu işletmeyi "
                "veya ürünü tanıtacak, influencer'ın kanalına uygun TEK bir kısa video konusu üret. "
                "İşletmenin adını ve en dikkat çekici özelliğini kullan. SADECE konu cümlesini yaz, "
                "açıklama ekleme. En fazla 18 kelime.\n\n"
                f"Karakter:\n{character_details}\n\n"
                f"Sayfa metni:\n{page_text}\n\n"
                f"Video dili: {language}"
            )
            topic = self.openai.chat(
                [{"role": "user", "content": prompt}], max_tokens=150
            ).strip().strip('"').strip("“”").splitlines()[0]
            log.info(f"Link'ten konu çıkarıldı: {topic}")
            return topic, f"🔗 Linkten konu çıkarıldı:\n“{topic}”"

        # Sayfa okunamadı → URL'den makul konu + uyarı
        prompt = (
            "Bir kullanıcı tanıtım için bu linki verdi ama sayfa içeriği okunamadı: "
            f"{url}\n\n"
            "Linkteki alan adı/ipuçlarından yola çıkarak influencer'ın kanalına uygun, makul ve "
            "ilgi çekici TEK bir video konusu öner. SADECE konu cümlesini yaz. En fazla 18 kelime.\n\n"
            f"Karakter:\n{character_details}\nVideo dili: {language}"
        )
        topic = self.openai.chat(
            [{"role": "user", "content": prompt}], max_tokens=150
        ).strip().strip('"').strip("“”").splitlines()[0]
        log.info(f"Link okunamadı, URL'den konu üretildi: {topic}")
        note = (
            "⚠️ Linkteki sayfayı okuyamadım (muhtemelen Instagram/harita gibi JS-ağır bir sayfa).\n"
            f"Onun yerine şu konuyu kullandım:\n“{topic}”\n"
            "Daha isabetli olması için mekan/ürünü kısaca yazıp tekrar deneyebilirsin."
        )
        return topic, note
