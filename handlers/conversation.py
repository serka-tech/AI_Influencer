from __future__ import annotations

"""
Conversation Handlers — Telegram Form Akışı
=============================================
/baslat → mod seçimi → (mekan videosu) → konu/input → sahne sayısı

Bu modül ConversationHandler'ın ilk aşamalarını yönetir:
giriş, mod, mekan videosu, konu girdisi, sahne seçimi ve iptal/yardım.
"""

import asyncio

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import ContextTypes, ConversationHandler

from config import settings
from logger import get_logger
from handlers.shared import (
    ASK_MODE,
    ASK_VENUE_VIDEO,
    ASK_INPUT,
    ASK_BGM,
    authorized,
    task_manager,
)
from bot_services import kie
from core.topic_resolver import is_url, is_negative

log = get_logger("handlers.conversation")


# ─────────────────────────────────────
# /baslat — giriş noktası
# ─────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not authorized(update):
        await update.message.reply_text("⛔ Bu botu kullanma yetkin yok.")
        return ConversationHandler.END

    context.user_data.clear()
    buttons = [[
        InlineKeyboardButton("📹 Normal video", callback_data="mode:normal"),
        InlineKeyboardButton("📍 Mekan tanıtımı", callback_data="mode:venue"),
    ]]
    await update.message.reply_text(
        "🎬 *Melisa — video üretimi*\n\n"
        "Nasıl bir video üretelim?\n"
        "• 📹 *Normal video* — konu/link/otomatik fikir\n"
        "• 📍 *Mekan tanıtımı* — bir mekanın videosunu yükle, Melisa o mekanı tanıtsın\n\n"
        "(İstediğin an /iptal)",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ASK_MODE


# ─────────────────────────────────────
# Mod seçimi (normal / mekan)
# ─────────────────────────────────────
async def on_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    mode = query.data.split(":", 1)[1]
    context.user_data["venue_mode"] = (mode == "venue")

    if mode == "venue":
        await query.edit_message_text("📍 Mekan tanıtımı seçildi.")
        await query.message.reply_text(
            "🎥 Tanıtmak istediğin mekanın *videosunu gönder* (Telegram'dan video yükle).\n\n"
            "Mekanın içini/dışını gösteren kısa bir video yeterli. "
            "Videoyu alınca mekanı senaryoya işleyeceğim.",
            parse_mode="Markdown",
        )
        return ASK_VENUE_VIDEO

    await query.edit_message_text("📹 Normal video seçildi.")
    return await _ask_input(query.message, context)


# ─────────────────────────────────────
# Mekan videosu alma
# ─────────────────────────────────────
async def on_venue_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    video = msg.video or (msg.document if msg.document and (msg.document.mime_type or "").startswith("video") else None)
    if not video:
        await msg.reply_text(
            "⚠️ Bu bir video değil. Lütfen mekanın *videosunu* gönder "
            "(Telegram'dan video olarak yükle).",
            parse_mode="Markdown",
        )
        return ASK_VENUE_VIDEO

    await msg.reply_text("📥 Mekan videosu alınıyor...")
    try:
        tg_file = await context.bot.get_file(video.file_id)
        # PTB v20: file_path tam HTTPS URL'dir
        telegram_url = tg_file.file_path
        # Kalıcı URL için Kie dosya sunucusuna yükle (başarısızsa Telegram URL'i kullan)
        try:
            venue_url = await asyncio.to_thread(
                kie.upload_file_from_url, telegram_url, "venue_video.mp4", "videos/user-uploads"
            )
        except Exception:
            log.warning("Mekan videosu Kie'ye yüklenemedi, Telegram URL kullanılacak", exc_info=True)
            venue_url = telegram_url

        context.user_data["venue_video_url"] = venue_url
        context.user_data["venue_video_file_id"] = video.file_id
        log.info(f"Mekan videosu kaydedildi: {venue_url[:80]}")
        await msg.reply_text(
            "✅ Mekan videosu kaydedildi.\n"
            "_(Not: Bu fazda video senaryoyu mekana göre yazmak için kullanılıyor; "
            "karakteri videodaki mekana yerleştirme 3. fazda devreye girecek.)_",
            parse_mode="Markdown",
        )
    except Exception as e:
        log.error("Mekan videosu işlenemedi", exc_info=True)
        await msg.reply_text(f"❌ Videoyu işleyemedim: {e}\nTekrar göndermeyi dene ya da /iptal.")
        return ASK_VENUE_VIDEO

    return await _ask_input(msg, context)


# ─────────────────────────────────────
# Konu/girdi sorusu
# ─────────────────────────────────────
async def _ask_input(message, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Konu/girdi sorusunu gönderir. Mekan modunda mekan bilgisini ister."""
    if context.user_data.get("venue_mode"):
        await message.reply_text(
            "📝 Mekan hakkında kısa bilgi ver:\n"
            "• Mekanın *adı* ve öne çıkarmak istediğin şeyler\n"
            "  (örn: \u201cMarin Cafe — deniz manzarası, taze deniz ürünleri, gün batımı\u201d)\n"
            "• İstersen mekanın *web/menü linkini* yapıştır\n"
            "• Bilgi yoksa *\"yok\"* yaz, videoya ve mekana göre ben kurgulayayım",
            parse_mode="Markdown",
        )
    else:
        await message.reply_text(
            "Tanıtmak istediğin bir şey var mı?\n"
            "• 🔗 Mekan/ürün *linki* yapıştır (web sitesi, menü sayfası)\n"
            "• ✍️ Konuyu *kısaca yaz*, senaryoyu ben yazayım (örn: \u201cdeniz manzaralı yeni bir kafe\u201d)\n"
            "• 📝 Hazır *konuşma metnini yapıştır*, onu birebir seslendireyim\n"
            "• 🤖 Hiçbir şey yoksa *\"yok\"* yaz, konuyu ben kendim bulayım",
            parse_mode="Markdown",
        )
    return ASK_INPUT


async def on_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Ham giriş — konu çözümü üretim aşamasında yapılır (LLM/fetch zaman alabilir)
    raw = update.message.text.strip()
    context.user_data["raw_input"] = raw
    context.user_data["language"] = "Türkçe"

    # Girdiyi sınıflandır: kendi metni (script) mi, konu mu?
    # URL / negatif ("yok") / kısa metin → konu modu: araştırma + genişletme
    #   sahne sayısını belirler.
    # Yeterince uzun düz metin → script modu: metin birebir sahnelere bölünür ve aynen seslendirilir.
    if (
        not context.user_data.get("venue_mode")
        and not is_url(raw)
        and not is_negative(raw)
        and len(raw.split()) >= settings.SCRIPT_MODE_MIN_WORDS
    ):
        context.user_data["input_mode"] = "script"
    else:
        context.user_data["input_mode"] = "topic"

    # Sahne sayısı artık otomatik belirleniyor; doğrudan müzik sorusuna geç.
    return await _ask_bgm(update.message, context)


# ─────────────────────────────────────
# Arka plan müziği sorusu
# ─────────────────────────────────────
async def _ask_bgm(message, context: ContextTypes.DEFAULT_TYPE) -> int:
    buttons = [[
        InlineKeyboardButton("🎵 Evet, müzik ekle", callback_data="bgm:yes"),
        InlineKeyboardButton("🔇 Hayır, sessiz", callback_data="bgm:no"),
    ]]
    await message.reply_text(
        "Videonun arka planına hafif bir müzik ekleyelim mi?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ASK_BGM


# ─────────────────────────────────────
# Arka Plan Müziği Seçimi
# ─────────────────────────────────────
async def on_bgm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    data = query.data.split(":")[1]
    context.user_data["use_bgm"] = (data == "yes")
    
    msg = "🎵 Müzik eklenecek." if data == "yes" else "🔇 Müzik eklenmeyecek."
    await query.edit_message_text(msg)

    # Senaryoyu üret ve onaya sun
    from handlers.scenario import build_scenario
    return await build_scenario(query.message.chat_id, context)


# ─────────────────────────────────────
# İptal ve yardım
# ─────────────────────────────────────
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Arka planda üretim sürüyorsa onu da durdur
    chat_id = update.effective_chat.id
    cancelled_production = task_manager.cancel(chat_id, context)

    context.user_data.clear()
    if cancelled_production:
        await update.message.reply_text(
            "🛑 Üretim durduruldu. /baslat ile yeniden başlayabilirsin.\n"
            "ℹ️ Başlamış Kie işleri varsa başarısız sayılır ve kredi iade edilir."
        )
    else:
        await update.message.reply_text("❌ İptal edildi. /baslat ile yeniden başlayabilirsin.")
    return ConversationHandler.END


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🎬 *AI Influencer Bot*\n\n"
        "/baslat — yeni video üret\n"
        "/iptal — akışı iptal et\n\n"
        "İki mod var:\n"
        "• 📹 *Normal video* — konu/link/otomatik fikir\n"
        "• 📍 *Mekan tanıtımı* — mekanın videosunu yükle, Melisa o mekanı tanıtsın\n\n"
        "Senaryo ve maliyet, üretimden önce onayına sunulur.\n"
        f"Karakter ve referans görsel config'de sabit. "
        f"Sahne aralığı: {settings.MIN_SCENES}-{settings.MAX_SCENES}.",
        parse_mode="Markdown",
    )
