from __future__ import annotations

"""
Publish Handler — Yayın Onayı
================================
Video üretimi tamamlandıktan sonra kullanıcının yayın onayını/reddini yönetir.
Onay verilirse Upload-Post ile TikTok/Instagram/YouTube'a yükler.
"""

import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from config import settings
from logger import get_logger
from core.caption_generator import CaptionGenerator
from bot_services import upload_post

log = get_logger("handlers.publish")


# ─────────────────────────────────────
# Yardımcı: onay mesajını güncelle
# ─────────────────────────────────────
async def _status(query, text: str) -> None:
    """Onay mesajını günceller — caption'lı veya düz metin mesajı fark etmeden çalışır."""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    try:
        await query.message.reply_text(text)
    except Exception:
        log.warning("Durum mesajı gönderilemedi", exc_info=True)


# ─────────────────────────────────────
# Yayın onay/red
# ─────────────────────────────────────
async def on_publish_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    decision = query.data.split(":")[1]

    if decision == "no":
        await _status(query, "❌ Video yayınlanmadı.")
        return

    video_url = context.user_data.get("final_video_url")
    caption = context.user_data.get("publish_caption", "")
    hashtags = context.user_data.get("hashtags", [])
    if not video_url:
        await _status(query, "⚠️ Video bulunamadı, /baslat ile tekrar dene.")
        return

    if not upload_post:
        await _status(
            query,
            "ℹ️ Yayınlama kapalı (UPLOAD_POST_API_KEY tanımlı değil). "
            "Video ve açıklama yukarıda hazır, elle paylaşabilirsin.",
        )
        return

    await _status(query, "📡 Yayınlanıyor...")
    try:
        platforms = settings.PUBLISH_PLATFORMS
        # Per-platform caption + hashtag (upload_post limitleri kendi uygular)
        captions = {p: {"caption": caption, "hashtags": hashtags} for p in platforms}
        if "instagram" in platforms:
            captions["instagram"]["media_type"] = "REELS"
        if "youtube" in platforms:
            captions["youtube"] = {
                "title": caption.splitlines()[0][:90] if caption else "Melisa",
                "description": CaptionGenerator.format_for_telegram(caption, hashtags),
                "tags": [h.lstrip("#") for h in hashtags],
            }
        result = await asyncio.to_thread(
            upload_post.upload_video, video_url, platforms, captions
        )
        await query.message.reply_text(
            f"✅ Yayınlandı: {', '.join(platforms)}\n(request_id: {result.get('request_id', '-')})"
        )
    except Exception as e:
        log.error("Yayınlama hatası", exc_info=True)
        await query.message.reply_text(f"❌ Yayınlama başarısız: {e}")
