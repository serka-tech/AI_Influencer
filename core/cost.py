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
VEO_FAST_CREDITS = 312
CREDITS_PER_SCENE = NANO_BANANA_PRO_CREDITS + VEO_FAST_CREDITS  # 330

# Kredi → USD (yaklaşık; env ile ayarlanabilir). Tipik Kie paketleri ~$0.00125/kredi.
KIE_CREDIT_TO_USD = float(os.environ.get("KIE_CREDIT_TO_USD", "0.00125"))

# Ek servisler (yaklaşık, USD)
REPLICATE_CONCAT_USD = 0.01   # çok sahnede video birleştirme (tek sahnede yok)
OPENAI_FLAT_USD = 0.03        # senaryo + konu + caption üretimi (toplam)


def estimate_cost(scene_count: int) -> dict:
    """Verilen sahne sayısı için maliyet tahmini döner."""
    kie_credits = CREDITS_PER_SCENE * scene_count
    kie_usd = kie_credits * KIE_CREDIT_TO_USD
    replicate_usd = REPLICATE_CONCAT_USD if scene_count > 1 else 0.0
    openai_usd = OPENAI_FLAT_USD
    total_usd = kie_usd + replicate_usd + openai_usd
    return {
        "scene_count": scene_count,
        "kie_credits": kie_credits,
        "kie_usd": round(kie_usd, 3),
        "replicate_usd": round(replicate_usd, 3),
        "openai_usd": round(openai_usd, 3),
        "total_usd": round(total_usd, 3),
    }


def format_cost(cost: dict, balance: float | None = None) -> str:
    """Telegram için maliyet özeti metni."""
    lines = [
        "💰 *Tahmini maliyet*",
        f"• Kie: {cost['kie_credits']} kredi (~${cost['kie_usd']:.2f})",
    ]
    if cost["replicate_usd"]:
        lines.append(f"• Birleştirme: ~${cost['replicate_usd']:.2f}")
    lines.append(f"• AI metin: ~${cost['openai_usd']:.2f}")
    lines.append(f"*Toplam: ~${cost['total_usd']:.2f}* ({cost['scene_count']} sahne)")
    if balance is not None:
        lines.append(f"\n🏦 Kalan Kie bakiyesi: {balance:.0f} kredi")
    lines.append("\n_Kredi miktarı kesin, USD yaklaşıktır._")
    return "\n".join(lines)
