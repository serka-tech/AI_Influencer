from __future__ import annotations

"""
Cost Handler — Maliyet Gösterimi ve Üretim Tetikleme
======================================================
Senaryo onaylandıktan sonra tahmini maliyeti gösterir ve
kullanıcı onaylarsa üretimi arka planda başlatır.
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
from core.cost import estimate_cost, format_cost
from handlers.shared import COST_REVIEW, task_manager
from bot_services import kie

log = get_logger("handlers.cost")


# ─────────────────────────────────────
# Maliyet gösterimi
# ─────────────────────────────────────
async def show_cost(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Senaryo onaylandıktan sonra tahmini maliyeti gösterir ve onay butonlarını sunar."""
    scene_count = context.user_data["scenes"]
    cost = estimate_cost(scene_count, settings.VIDEO_ENGINE)
    try:
        balance_data = await asyncio.to_thread(kie.get_credit_balance)
        balance = balance_data.get("data") if isinstance(balance_data, dict) else None
    except Exception:
        log.warning("Bakiye alınamadı", exc_info=True)
        balance = None

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Onayla, üret", callback_data="cost:approve"),
        InlineKeyboardButton("❌ İptal", callback_data="cost:cancel"),
    ]])
    await context.bot.send_message(
        chat_id,
        format_cost(cost, balance) + "\n\nÜretime başlayayım mı?",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    return COST_REVIEW


# ─────────────────────────────────────
# Maliyet onay/iptal
# ─────────────────────────────────────
async def on_cost_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    decision = query.data.split(":", 1)[1]

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if decision == "cancel":
        await query.message.reply_text("❌ İptal edildi. /baslat ile yeniden başlayabilirsin.")
        return ConversationHandler.END

    await query.message.reply_text("🚀 Üretim başlıyor... (durdurmak için /iptal)")
    # Uzun iş arka planda; conversation'dan çık. Task'ı sakla ki /iptal durdurabilsin.
    chat_id = update.effective_chat.id
    from handlers.production import run_production
    task = asyncio.create_task(run_production(update, context))
    task_manager.start(chat_id, task, context)
    return ConversationHandler.END
