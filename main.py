from __future__ import annotations

"""
AI Influencer — Telegram Bot Entry Point
==========================================
n8n "SKOOL - Çoklu Sahne Veo 3.1" akışının kod karşılığı.

Akış:
  /baslat → konu sor → dil sor → sahne sayısı sor (inline)
  → senaryo üret → senaryoyu göster + ONAY iste (✅ Onayla / ✏️ Düzenle / 🔄 Yeniden üret)
  → onaylanınca maliyeti göster + ONAY iste (✅ Onayla, üret / ❌ İptal)
  → sahne sahne görsel+video → birleştir
  → videoyu gönder → onay sor (✅ Yayınla / ❌ Yayınlama)
  → onaylanırsa upload-post ile TikTok + Instagram + YouTube'a yükle

Handler modülleri:
  handlers/conversation.py  — form akışı (/baslat, mod, input, sahne, iptal, yardım)
  handlers/scenario.py      — senaryo üretim, onay, düzenleme
  handlers/cost.py           — maliyet gösterim + üretim tetikleme
  handlers/production.py     — video üretim pipeline
  handlers/publish.py        — yayın onayı
  handlers/shared.py         — ortak sabitler ve yardımcılar
  bot_services.py            — merkezi servis başlatma
"""

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    PicklePersistence,
)

from config import settings
from logger import get_logger

# Handler modülleri
from handlers.shared import (
    ASK_MODE,
    ASK_VENUE_VIDEO,
    ASK_INPUT,
    SCENARIO_REVIEW,
    SCENARIO_EDIT,
    COST_REVIEW,
    ASK_BGM,
    task_manager,
)
from handlers.conversation import (
    cmd_start,
    on_mode,
    on_venue_video,
    on_input,
    on_bgm,
    cmd_cancel,
    cmd_help,
)
from handlers.scenario import on_scenario_decision, on_scenario_edit
from handlers.cost import on_cost_decision
from handlers.publish import on_publish_decision

log = get_logger("main")


def main() -> None:
    log.info("AI Influencer bot başlıyor...")
    log.info(f"Yayınlama: {'açık' if settings.PUBLISH_ENABLED else 'kapalı'} | "
             f"Senaryo modeli: {settings.OPENAI_SCENARIO_MODEL} | "
             f"Referans: {settings.REFERENCE_IMAGE_URL[:60]}")

    persistence = PicklePersistence(filepath="ai_influencer_bot_state.pickle")
    app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).persistence(persistence).build()

    conv = ConversationHandler(
        name="ai_influencer_conv",
        persistent=True,
        allow_reentry=True,
        entry_points=[
            CommandHandler("baslat", cmd_start),
            CommandHandler("start", cmd_start)
        ],
        states={
            ASK_MODE: [CallbackQueryHandler(on_mode, pattern=r"^mode:")],
            ASK_VENUE_VIDEO: [
                MessageHandler(filters.VIDEO | filters.Document.VIDEO, on_venue_video),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_venue_video),
            ],
            ASK_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_input)],
            ASK_BGM: [CallbackQueryHandler(on_bgm, pattern="^bgm:")],
            SCENARIO_REVIEW: [CallbackQueryHandler(on_scenario_decision, pattern="^scenario:")],
            SCENARIO_EDIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_scenario_edit)],
            COST_REVIEW: [CallbackQueryHandler(on_cost_decision, pattern=r"^cost:")],
        },
        fallbacks=[CommandHandler("iptal", cmd_cancel)],
    )
    app.add_handler(conv)
    # Konuşma bittikten sonra (üretim arka planda sürerken) de /iptal yanıt versin
    app.add_handler(CommandHandler("iptal", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_publish_decision, pattern=r"^publish:"))
    app.add_handler(CommandHandler("yardim", cmd_help))
    app.add_handler(CommandHandler("help", cmd_help))

    # Global hata yakalayıcı (unhandled exceptions için admin bildirimi)
    from handlers.shared import global_error_handler
    app.add_error_handler(global_error_handler)

    # Restart bildirimi: yarım kalmış üretimlerin kullanıcılarına bildir
    async def post_init(application: Application) -> None:
        # bot_data persistence'tan yüklendikten sonra çalışır
        await task_manager.restart_notify(application)

    app.post_init = post_init
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
