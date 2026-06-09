from __future__ import annotations

"""
Caption Generator — Keşfet/Reels Odaklı Açıklama + Hashtag
============================================================
Yayın için (Instagram Reels öncelikli) keşfete düşmeyi hedefleyen bir açıklama metni ve
hashtag seti üretir. Senaryo motorunun ürettiği kısa on-screen caption'dan ayrıdır;
bu metin paylaşım açıklaması olarak kullanılır.

Çıktı:
    {
      "caption": "hook + değer + yumuşak CTA (konuşma dilinde, 2-4 kısa satır)",
      "hashtags": ["#kesfet", "#mersin", ...]   # keşfet + yerel + niş karışımı
    }
"""

import re

from logger import get_logger

log = get_logger("caption_generator")

# Her zaman eklenecek keşfet/erişim hashtag çekirdeği (Reels akışı için)
_DISCOVERY_CORE = ["#kesfet", "#keşfet", "#reels", "#reelstürkiye", "#explore"]

_SYSTEM = (
    "Sen bir Instagram Reels büyüme ve keşfet (Explore) uzmanısın. Verilen video için, "
    "keşfete düşme ve erişim alma olasılığını en üst düzeye çıkaracak bir paylaşım açıklaması "
    "ve hashtag seti üretirsin. Çıktı SADECE geçerli JSON olmalı.\n\n"
    "JSON şeması:\n"
    '{ "caption": string, "hashtags": [string, ...] }\n\n'
    "CAPTION KURALLARI:\n"
    "- Belirtilen konuşma dilinde yaz.\n"
    "- İlk satır güçlü bir hook olsun (merak/şaşırtma/fayda vaadi).\n"
    "- 2-4 kısa satır; akıcı, samimi, satış gibi değil.\n"
    "- Sonunda yumuşak bir etkileşim çağrısı olsun (kaydet / yorumla / paylaş gibi).\n"
    "- 1-3 emoji kullan. Link, @mention veya hashtag KOYMA (hashtag'ler ayrı alanda).\n"
    "- Tire (—) kullanma, kısa cümleler kur.\n\n"
    "HASHTAG KURALLARI:\n"
    "- Toplam 12-18 hashtag.\n"
    "- Karışım: yerel/şehir etiketleri + konuya özel niş etiketler + birkaç geniş erişim etiketi.\n"
    "- Hepsi küçük harf, boşluksuz, Türkçe karakterler serbest. Yasaklı/spam etiketlerden kaçın.\n"
    "- Konu ve şehirle gerçekten alakalı olsun; alakasız popüler etiket ekleme.\n"
    "- '#' ile başlasın."
)


def _norm_hashtag(tag: str) -> str:
    tag = tag.strip()
    if not tag:
        return ""
    tag = tag.lstrip("#")
    tag = re.sub(r"\s+", "", tag)
    tag = re.sub(r"[^0-9A-Za-zğüşöçıİĞÜŞÖÇ_]", "", tag)
    return f"#{tag.lower()}" if tag else ""


class CaptionGenerator:
    def __init__(self, openai_service):
        self.openai = openai_service

    def generate(
        self,
        topic: str,
        character_details: str,
        language: str,
        max_hashtags: int = 20,
    ) -> dict:
        """Keşfet odaklı caption + hashtag üretir. Hata olursa makul bir fallback döner."""
        user = (
            f"Video konusu: {topic}\n"
            f"Konuşma dili: {language}\n\n"
            f"Influencer karakteri (ton/şehir için):\n{character_details}\n\n"
            "Bu videoyu Instagram Reels'te keşfete taşıyacak açıklama ve hashtag'leri üret."
        )
        try:
            result = self.openai.chat_json(
                [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
                max_tokens=900,
            )
            caption = str(result.get("caption", "")).strip()
            raw_tags = result.get("hashtags", []) or []
            tags = []
            for t in raw_tags:
                nt = _norm_hashtag(str(t))
                if nt and nt not in tags:
                    tags.append(nt)
            # Keşfet çekirdeğini GARANTİ et: eksik olanları başa al, sonra cap uygula.
            # Böylece limit dolsa bile erişim etiketleri korunur.
            missing_core = [c for c in _DISCOVERY_CORE if c not in tags]
            tags = (missing_core + tags)[:max_hashtags]

            if not caption:
                raise ValueError("Boş caption")
            log.info(f"Keşfet caption üretildi: {len(caption)} kr, {len(tags)} hashtag")
            return {"caption": caption, "hashtags": tags}

        except Exception:
            log.error("Caption üretimi başarısız, fallback kullanılıyor", exc_info=True)
            return {
                "caption": topic.strip(),
                "hashtags": _DISCOVERY_CORE + ["#mersin"],
            }

    @staticmethod
    def format_for_telegram(caption: str, hashtags: list[str]) -> str:
        """Telegram'da gösterim için caption + hashtag birleşik metni."""
        tags = " ".join(hashtags)
        return f"{caption}\n\n{tags}" if tags else caption
