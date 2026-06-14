from __future__ import annotations

"""
Shared — Handler'lar Arası Ortak Sabitler ve Yardımcılar
=========================================================
Conversation state'leri, yetki kontrolü, uzun mesaj gönderimi ve
indirme gibi cross-cutting utility'ler burada tanımlıdır.
"""

import traceback
import requests
from telegram import Update
from telegram.ext import ContextTypes

from config import settings
from logger import get_logger

log = get_logger("handlers.shared")

# ── Conversation state'leri ──
(
    ASK_MODE,
    ASK_VENUE_VIDEO,
    ASK_INPUT,
    ASK_LANGUAGE,
    ASK_SCENES,
    SCENARIO_REVIEW,
    SCENARIO_EDIT,
    COST_REVIEW,
    ASK_BGM,
) = range(9)

# Sahne süresi motora göre: Veo 3.1 ~8 sn, Kling 3.0 ~5 sn
SECONDS_PER_SCENE = 8 if settings.VIDEO_ENGINE == "veo" else 5


# ─────────────────────────────────────
# Task Manager — Restart-Safe Üretim Takibi
# ─────────────────────────────────────
class TaskManager:
    """
    Aktif üretim task'larını yönetir. İki katmanlı:
    - Bellek: asyncio.Task referansları (/iptal ile cancel için)
    - Persistence: bot_data['active_productions'] ile chat_id kaydı
      → Railway restart sonrası yarım kalan üretimler tespit edilip
        kullanıcıya bildirilir.
    """

    BOT_DATA_KEY = "active_productions"

    def __init__(self):
        self._tasks: dict[int, object] = {}  # chat_id → asyncio.Task

    def start(self, chat_id: int, task, context) -> None:
        """Yeni üretim task'ı kaydet (bellek + persistence)."""
        self._tasks[chat_id] = task
        prods = context.bot_data.setdefault(self.BOT_DATA_KEY, {})
        prods[chat_id] = True
        log.info(f"Üretim başlatıldı: chat_id={chat_id}")

    def finish(self, chat_id: int, context) -> None:
        """Üretim tamamlandı — kayıtları temizle."""
        self._tasks.pop(chat_id, None)
        prods = context.bot_data.get(self.BOT_DATA_KEY, {})
        prods.pop(chat_id, None)
        log.info(f"Üretim tamamlandı: chat_id={chat_id}")

    def cancel(self, chat_id: int, context) -> bool:
        """Çalışan task'ı iptal et. Dönen: task vardı ve iptal edildi mi."""
        task = self._tasks.pop(chat_id, None)
        prods = context.bot_data.get(self.BOT_DATA_KEY, {})
        prods.pop(chat_id, None)
        if task and not task.done():
            task.cancel()
            log.info(f"Üretim iptal edildi: chat_id={chat_id}")
            return True
        return False

    def is_running(self, chat_id: int) -> bool:
        task = self._tasks.get(chat_id)
        return task is not None and not task.done()

    def get_stale_chat_ids(self, context) -> list[int]:
        """
        Restart sonrası: persistence'ta kayıtlı ama bellekte task'ı olmayan
        (yani restart ile kesilen) chat_id'leri döner.
        """
        prods = context.bot_data.get(self.BOT_DATA_KEY, {})
        stale = [cid for cid in prods if cid not in self._tasks]
        return stale

    async def restart_notify(self, application) -> None:
        """
        Bot açılışında çağrılır (post_init callback).
        Restart ile kesilmiş üretimlerin kullanıcılarına bildirim gönderir
        ve persistence kayıtlarını temizler.

        Args:
            application: telegram.ext.Application instance (PTB v20 post_init signature)
        """
        prods = application.bot_data.get(self.BOT_DATA_KEY, {})
        stale = [cid for cid in prods if cid not in self._tasks]
        if not stale:
            return

        bot = application.bot
        for chat_id in stale:
            try:
                await bot.send_message(
                    chat_id,
                    "⚠️ Bot yeniden başlatıldı. Devam eden video üretimin kesildi.\n"
                    "ℹ️ Başlamış Kie işleri varsa kredi iade edilir.\n"
                    "/baslat ile yeniden başlayabilirsin.",
                )
                log.info(f"Restart bildirimi gönderildi: chat_id={chat_id}")
            except Exception:
                log.warning(f"Restart bildirimi gönderilemedi: chat_id={chat_id}", exc_info=True)
        # Temizle
        for cid in stale:
            prods.pop(cid, None)


# Singleton instance — tüm handler'lar bunu kullanır
task_manager = TaskManager()

# Geriye uyumluluk: eski running_tasks referansları için
running_tasks = task_manager._tasks


# ─────────────────────────────────────
# Erişim kontrolü
# ─────────────────────────────────────
def authorized(update: Update) -> bool:
    """Kullanıcının botu kullanma yetkisi var mı kontrol eder."""
    if not settings.ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return bool(user and user.id in settings.ALLOWED_USER_IDS)


# ─────────────────────────────────────
# Uzun metin gönderimi
# ─────────────────────────────────────
async def send_long(bot, chat_id: int, text: str, limit: int = 3800) -> None:
    """Uzun metni Telegram limitine sığacak şekilde satır bazında bölerek gönderir (düz metin)."""
    buf = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if buf:
                await bot.send_message(chat_id, buf)
                buf = ""
            await bot.send_message(chat_id, line[:limit])
            line = line[limit:]
        if len(buf) + len(line) + 1 > limit:
            await bot.send_message(chat_id, buf)
            buf = ""
        buf += line + "\n"
    if buf.strip():
        await bot.send_message(chat_id, buf)


from utils.retry import retry_api_call

# ─────────────────────────────────────
# Dosya indirme
# ─────────────────────────────────────
@retry_api_call(max_retries=3, base_delay=2.0, operation_name="Download")
def download(url: str) -> bytes:
    """URL'den dosya indirir ve byte dizisi döner (otomatik retry destekli)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    resp = requests.get(url, headers=headers, timeout=120)
    resp.raise_for_status()
    return resp.content


# ─────────────────────────────────────
# Admin Bildirimleri ve Hata Yönetimi
# ─────────────────────────────────────
async def notify_admin(bot, message: str) -> None:
    """Belirlenen admin chat_id'sine (varsa) bildirim gönderir."""
    if settings.ADMIN_CHAT_ID:
        try:
            await bot.send_message(
                settings.ADMIN_CHAT_ID, 
                f"🚨 *Sistem Bildirimi*\n\n{message}", 
                parse_mode="Markdown"
            )
        except Exception:
            log.error("Admin bildirimi gönderilemedi", exc_info=True)


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    PTB (python-telegram-bot) geneli, beklenmeyen hataları (unhandled exceptions) yakalar.
    Admin'e stack trace ile bildirir ve loglar.
    """
    log.error(f"Beklenmeyen Telegram hatası: {context.error}", exc_info=context.error)
    
    # Hata detayı (kısmi stack trace)
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)
    
    error_msg = (
        f"Kritik Telegram Hatası: `{context.error}`\n\n"
        f"```python\n{tb_string[-3000:]}\n```"
    )
    await notify_admin(context.bot, error_msg)
    
    # Kullanıcıya (hata eğer bir etkileşimden geldiyse) sessizce jenerik uyarı göster
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                update.effective_chat.id, 
                "❌ Sistemsel bir hata oluştu ve yöneticilere bildirildi. Lütfen daha sonra tekrar deneyin."
            )
        except Exception:
            pass
