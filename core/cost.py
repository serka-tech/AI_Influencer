from __future__ import annotations

"""
Cost — Video Maliyet Hesaplama
================================
eCom projesindeki maliyet gösterimi mantığının bu pipeline'a uyarlanmış hali.

Birim maliyetler 2026-05-31'de canlı ölçüldü (1K, 9:16):
- Nano Banana Pro (görsel) : 18 Kie kredisi / görsel
- Veo 3.1 fast (8 sn video) : 312 Kie kredisi / video
→ Sahne başı: 330 Kie kredisi

Kie kredisinin USD karşılığı satın alma paketine göre değişir; bu yüzden kredi miktarı
KESİN (ölçülen), USD ise YAKLAŞIK ve `KIE_CREDIT_TO_USD` ile ayarlanabilir.
Replicate concat ve OpenAI (senaryo + caption) küçük sabit kalemler olarak eklenir.
"""

import os

# Kie kredi birim maliyetleri (1K, 9:16)
NANO_BANANA_PRO_CREDITS = 18
VEO_VIDEO_CREDITS = 312   # Veo 3.1 fast, 8 sn, kendi sesiyle
KLING_VIDEO_CREDITS = 75  # Kling 3.0 (sessiz, ~5 sn) -> 14 kredi/sn * 5 + tampon

# Kredi → USD (yaklaşık; env ile ayarlanabilir). Tipik Kie paketleri ~$0.00125/kredi.
KIE_CREDIT_TO_USD = float(os.environ.get("KIE_CREDIT_TO_USD", "0.00125"))

# Ek servisler (yaklaşık, USD)
REPLICATE_CONCAT_USD = 0.01   # çok sahnede video birleştirme
OPENAI_FLAT_USD = 0.03        # senaryo + konu + caption üretimi
REPLICATE_LIPSYNC_USD = 0.02  # sahne başı Lip-Sync (sadece kling)
ELEVENLABS_USD = 0.01         # sahne başı ses üretimi (sadece kling)

def estimate_cost(scene_count: int, engine: str = "veo") -> dict:
    """Verilen sahne sayısı ve video motoru için maliyet tahmini döner."""
    engine = (engine or "veo").lower()
    video_credits = KLING_VIDEO_CREDITS if engine == "kling" else VEO_VIDEO_CREDITS
    kie_credits = (NANO_BANANA_PRO_CREDITS + video_credits) * scene_count
    kie_usd = kie_credits * KIE_CREDIT_TO_USD

    concat_usd = REPLICATE_CONCAT_USD if scene_count > 1 else 0.0
    if engine == "kling":
        replicate_usd = concat_usd + (REPLICATE_LIPSYNC_USD * scene_count)
        elevenlabs_usd = ELEVENLABS_USD * scene_count
    else:
        replicate_usd = concat_usd  # veo'da lip-sync yok
        elevenlabs_usd = 0.0
    openai_usd = OPENAI_FLAT_USD
    total_usd = kie_usd + replicate_usd + openai_usd + elevenlabs_usd
    return {
        "engine": engine,
        "scene_count": scene_count,
        "kie_credits": kie_credits,
        "kie_usd": round(kie_usd, 3),
        "replicate_usd": round(replicate_usd, 3),
        "elevenlabs_usd": round(elevenlabs_usd, 3),
        "openai_usd": round(openai_usd, 3),
        "total_usd": round(total_usd, 3),
    }

def format_cost(cost: dict, balance: float | None = None) -> str:
    """Telegram için maliyet özeti metni."""
    engine = cost.get("engine", "veo")
    video_label = "Görsel+Kling" if engine == "kling" else "Görsel+Veo"
    lines = [
        "💰 *Tahmini maliyet*",
        f"• Kie ({video_label}): {cost['kie_credits']} kredi (~${cost['kie_usd']:.2f})",
    ]
    if cost.get("elevenlabs_usd"):
        lines.append(f"• Ses (ElevenLabs): ~${cost['elevenlabs_usd']:.2f}")
    if cost["replicate_usd"]:
        label = "Lip-Sync & Birleştirme" if engine == "kling" else "Birleştirme"
        lines.append(f"• {label}: ~${cost['replicate_usd']:.2f}")
    lines.append(f"• AI senaryo: ~${cost['openai_usd']:.2f}")
    lines.append(f"*Toplam: ~${cost['total_usd']:.2f}* ({cost['scene_count']} sahne)")
    if balance is not None:
        lines.append(f"\n🏦 Kalan Kie bakiyesi: {balance:.0f} kredi")
    lines.append("\n_Kredi miktarı kesin, USD yaklaşıktır._")
    return "\n".join(lines)
