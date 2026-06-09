from __future__ import annotations

"""
Place Research — Gerçek Mekan/Koy Araştırması (anahtarsız)
===========================================================
"Mersin'de denize girilecek koylar" gibi liste/keşif konularında, internetten GERÇEK
mekan isimlerini bulur ve her mekan için bir referans görsel toplar. Hiçbir ücretli API
anahtarı gerektirmez:

- İsimler  : DuckDuckGo HTML arama sonuçları kazınır, LLM ile gerçek isimler çıkarılır.
- Görseller: Openverse (CC lisanslı, anahtarsız) → fallback Wikimedia Commons.

Her şey best-effort'tur; başarısız olursa boş/None döner ve çağıran taraf zarif şekilde
sabit karakter referansına / normal akışa düşer.
"""

import re
from urllib.parse import quote, urlparse

import requests

from logger import get_logger

log = get_logger("place_research")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "tr,en;q=0.8",
}

_IMG_OK = (".jpg", ".jpeg", ".png", ".webp")


def _strip_tags(html: str) -> str:
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?is)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def _scrape_search(query: str, max_chars: int = 6000) -> str:
    """DuckDuckGo HTML arama sonuçlarından düz metin toplar (best-effort)."""
    try:
        url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
        resp = requests.post(url, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
        # Sadece sonuç bağlantıları + snippet'ler önemli; tüm gövdeyi sadeleştir
        text = _strip_tags(resp.text)
        return text[:max_chars]
    except Exception:
        log.warning(f"Arama kazıma başarısız: {query[:60]}", exc_info=True)
        return ""


def _extract_names(query: str, snippets: str, openai_service, max_items: int) -> list[str]:
    """Arama metninden gerçek mekan isimlerini LLM ile çıkarır."""
    grounding = (
        f"Aşağıda bu arama için internet sonuçlarından çekilmiş metin var:\n\n{snippets}\n\n"
        if snippets and len(snippets) > 200
        else "İnternet sonucu çekilemedi; kendi güncel bilgine dayanarak GERÇEK ve var olan yerleri ver.\n\n"
    )
    prompt = (
        f"Kullanıcının konusu: \"{query}\".\n\n"
        f"{grounding}"
        f"Bu konuyla ilgili GERÇEK, var olan ve bilinen yer isimlerini çıkar. "
        f"En fazla {max_items} tane, en bilinenler önce. Uydurma isim verme. "
        "Sadece yerin kendi adını ver (şehir/bölge ekini tekrarlama). "
        'JSON döndür: {"places": ["isim1", "isim2", ...]}'
    )
    try:
        result = openai_service.chat_json(
            [{"role": "user", "content": prompt}], max_tokens=600
        )
        names = result.get("places") if isinstance(result, dict) else None
        if not isinstance(names, list):
            return []
        clean = []
        for n in names:
            if isinstance(n, str) and n.strip():
                clean.append(n.strip())
        log.info(f"Araştırma: {len(clean)} mekan bulundu — {clean}")
        return clean[:max_items]
    except Exception:
        log.warning("Mekan ismi çıkarımı başarısız", exc_info=True)
        return []


def _is_image_url(url: str) -> bool:
    if not isinstance(url, str) or not url.startswith("http"):
        return False
    path = urlparse(url).path.lower()
    return path.endswith(_IMG_OK)


def _image_from_openverse(query: str) -> str | None:
    """Openverse'ten (anahtarsız, CC lisanslı) bir referans görsel URL'i döner."""
    try:
        url = f"https://api.openverse.org/v1/images/?q={quote(query)}&page_size=5&mature=false"
        resp = requests.get(url, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("results", []):
            candidate = item.get("url") or item.get("thumbnail")
            if _is_image_url(candidate):
                return candidate
        # Uzantısı belirsiz olsa bile ilk geçerli url'i dene
        for item in data.get("results", []):
            candidate = item.get("url")
            if isinstance(candidate, str) and candidate.startswith("http"):
                return candidate
    except Exception:
        log.warning(f"Openverse görsel bulunamadı: {query[:60]}", exc_info=True)
    return None


def _image_from_wikimedia(query: str) -> str | None:
    """Wikimedia Commons'tan bir görsel URL'i döner (fallback)."""
    try:
        api = (
            "https://commons.wikimedia.org/w/api.php?action=query&format=json"
            "&generator=search&gsrnamespace=6&gsrlimit=5"
            f"&gsrsearch={quote(query)}"
            "&prop=imageinfo&iiprop=url&iiurlwidth=1024"
        )
        resp = requests.get(api, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
        pages = (resp.json().get("query", {}) or {}).get("pages", {}) or {}
        for page in pages.values():
            for info in page.get("imageinfo", []):
                candidate = info.get("thumburl") or info.get("url")
                if _is_image_url(candidate):
                    return candidate
    except Exception:
        log.warning(f"Wikimedia görsel bulunamadı: {query[:60]}", exc_info=True)
    return None


# Türkçe yer-tipi ekleri (görsel ararken isimden temizlenir)
_PLACE_SUFFIX_RE = re.compile(
    r"\b(plaj[ıi]?|koy[u]?|koylar[ıi]?|sahil[i]?|plajlar[ıi]?|beach|bay|cove)\b",
    re.IGNORECASE,
)


def _location_hint(context_query: str) -> str:
    """Sorgudan olası konum ipucunu çıkarır (ilk büyük harfle başlayan kelime)."""
    for tok in re.findall(r"\b[A-ZÇĞİÖŞÜ][a-zçğıöşü]+\b", context_query or ""):
        return tok
    return ""


def _find_reference_image(name: str, context_query: str) -> str | None:
    """Bir mekan için referans görsel arar: Openverse → Wikimedia → None. Birkaç varyant dener."""
    base = _PLACE_SUFFIX_RE.sub("", name).strip()
    loc = _location_hint(context_query)

    variants = [name]
    if base and base != name:
        variants.append(base)
    if loc and loc.lower() not in name.lower():
        variants.append(f"{base or name} {loc}")
    # Tekrarsız sırayla dene
    seen = set()
    for q in variants:
        q = q.strip()
        if not q or q in seen:
            continue
        seen.add(q)
        img = _image_from_openverse(q) or _image_from_wikimedia(q)
        if img:
            log.info(f"Referans görsel bulundu [{name}] (sorgu: {q}): {img[:80]}")
            return img
    log.info(f"Referans görsel bulunamadı [{name}] — karakter referansıyla devam edilecek")
    return None


def research_places(query: str, openai_service, max_items: int = 4) -> list[dict]:
    """
    Verilen konu için gerçek mekanları araştırır.

    Returns:
        [{"name": "<mekan adı>", "ref_image_url": "<url veya None>"}, ...]
        Hiç bulunamazsa boş liste.
    """
    snippets = _scrape_search(query)
    names = _extract_names(query, snippets, openai_service, max_items)
    if not names:
        return []

    places = []
    for name in names:
        ref = _find_reference_image(name, query)
        places.append({"name": name, "ref_image_url": ref})
    return places


def classify_discovery(query: str, openai_service) -> dict:
    """
    Konunun 'çok mekanlı liste/keşif' tipi olup olmadığını sınıflar.

    Returns:
        {"is_discovery": bool, "search_query": "<arama için sadeleştirilmiş sorgu>"}
    """
    prompt = (
        f"Kullanıcının video konusu: \"{query}\".\n\n"
        "Bu konu, BİRDEN FAZLA gerçek yerin (koylar, kafeler, plajlar, restoranlar, "
        "gezilecek yerler vb.) tek tek tanıtıldığı bir LİSTE/KEŞİF videosu mu? "
        "Örn: 'Mersin'de denize girilecek koylar' → evet. "
        "'Yeni bir kafe açılışı' veya 'sabah rutinim' → hayır.\n\n"
        'JSON döndür: {"is_discovery": true/false, "search_query": "internette aratmak için '
        'sadeleştirilmiş sorgu"}'
    )
    try:
        result = openai_service.chat_json(
            [{"role": "user", "content": prompt}], max_tokens=300
        )
        if isinstance(result, dict) and isinstance(result.get("is_discovery"), bool):
            return {
                "is_discovery": result["is_discovery"],
                "search_query": str(result.get("search_query") or query).strip(),
            }
    except Exception:
        log.warning("Keşif sınıflandırması başarısız", exc_info=True)
    return {"is_discovery": False, "search_query": query}
