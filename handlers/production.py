from __future__ import annotations

"""
Production Handler — Video Üretim Pipeline Çalıştırma
======================================================
Kullanıcı maliyeti onayladıktan sonra arka planda çalışır:
sahne sahne görsel+video üretir, birleştirir, açıklama+hashtag üretir
ve videoyu Telegram'a gönderir.
"""

import asyncio
import io

from telegram import Update, InputFile
from telegram.ext import ContextTypes
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import settings
from logger import get_logger, log_cost
from core.cost import estimate_cost
from core.caption_generator import CaptionGenerator
from core.production_pipeline import PipelineError
from handlers.shared import download, task_manager, notify_admin
from bot_services import pipeline, caption_generator

log = get_logger("handlers.production")


import re

# ─────────────────────────────────────
# Progress Tracker — İlerleme Bildirimi
# ─────────────────────────────────────
class ProgressTracker:
    def __init__(self, bot, chat_id: int, total_scenes: int):
        self.bot = bot
        self.chat_id = chat_id
        self.total_scenes = total_scenes
        self.message = None
        self.scenes_status = {i: "⏳ Bekliyor" for i in range(1, total_scenes + 1)}
        self.general_status = "🚀 Üretim başlatıldı"
        self._lock = asyncio.Lock()
        self._last_text = ""

    async def update(self, msg: str):
        async with self._lock:
            # "Sahne X" içeren mesajları ilgili sahneye ata, diğerlerini genel duruma ata
            m = re.search(r"Sahne (\d+)", msg)
            if m:
                idx = int(m.group(1))
                if 1 <= idx <= self.total_scenes:
                    self.scenes_status[idx] = msg
            else:
                self.general_status = msg

            # Tamamlanan sahneleri say (Veo için "videosu hazır", Kling için "tamamlandı!")
            completed = sum(
                1 for status in self.scenes_status.values() 
                if "tamamlandı!" in status or ("videosu hazır" in status and "Kling" not in status)
            )

            bar_len = 10
            filled = int((completed / self.total_scenes) * bar_len)
            bar = "▓" * filled + "░" * (bar_len - filled)

            lines = [
                f"🎬 *Üretim İlerlemesi* [{bar}] {completed}/{self.total_scenes}",
                f"ℹ️ _{self.general_status}_",
                ""
            ]
            for i in range(1, self.total_scenes + 1):
                status = self.scenes_status[i]
                if status == "⏳ Bekliyor":
                    lines.append(f"• Sahne {i}: ⏳ Bekliyor")
                else:
                    lines.append(f"• {status}")

            text = "\n".join(lines)
            if text == self._last_text:
                return

            try:
                if not self.message:
                    self.message = await self.bot.send_message(self.chat_id, text, parse_mode="Markdown")
                else:
                    await self.message.edit_text(text, parse_mode="Markdown")
            except Exception as e:
                # "Message is not modified" hatasını yoksay
                if "not modified" not in str(e).lower():
                    log.warning("Progress mesajı güncellenemedi", exc_info=True)
            self._last_text = text

    async def force_msg(self, msg: str):
        """Hata durumlarında yeni mesaj atmak için."""
        try:
            await self.bot.send_message(self.chat_id, msg, parse_mode="Markdown")
        except Exception:
            pass


# ─────────────────────────────────────
# Üretim pipeline'ı
# ─────────────────────────────────────
async def run_production(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Arka planda çalışan ana üretim fonksiyonu."""
    chat_id = update.effective_chat.id
    bot = context.bot
    language = context.user_data.get("language", "Turkish")
    topic = context.user_data["topic"]
    scenario = context.user_data["scenario"]
    scene_count = context.user_data.get("scenes", len(scenario.get("scenes", [])))

    tracker = ProgressTracker(bot, chat_id, scene_count)

    async def progress(msg: str) -> None:
        await tracker.update(msg)

    try:
        # Cache'i user_data'dan al, yoksa boş yarat
        image_cache = context.user_data.setdefault("image_cache", {})

        # Görsel + Video + Birleştirme (senaryo onaylı)
        use_bgm = context.user_data.get("use_bgm", False)
        final_video_url = await pipeline.run(
            scenario, 
            progress=progress, 
            image_cache=image_cache, 
            use_bgm=use_bgm
        )

        # 3) Keşfet odaklı açıklama + hashtag
        await progress("🏷️ Açıklama ve hashtag'ler hazırlanıyor...")
        publish = await asyncio.to_thread(
            caption_generator.generate, topic, settings.CHARACTER_DETAILS, language
        )
        publish_caption = publish["caption"]
        hashtags = publish["hashtags"]
        full_caption = CaptionGenerator.format_for_telegram(publish_caption, hashtags)

        # 4) Videoyu indir ve gönder
        await progress("📤 Video gönderiliyor...")
        video_bytes = await asyncio.to_thread(download, final_video_url)

        # Maliyeti logla
        scene_count = context.user_data.get("scenes", len(scenario.get("scenes", [])))
        actual_cost = estimate_cost(scene_count, settings.VIDEO_ENGINE)
        try:
            cost_float = float(actual_cost.get("max", 0.0))
            await asyncio.to_thread(log_cost, "AI_Influencer", cost_float, f"{scene_count} sahne video üretimi")
        except Exception:
            log.warning("Maliyet loglanamadı", exc_info=True)

        context.user_data["final_video_url"] = final_video_url
        context.user_data["publish_caption"] = publish_caption
        context.user_data["hashtags"] = hashtags

        approve_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yayınla", callback_data="publish:yes"),
            InlineKeyboardButton("❌ Yayınlama", callback_data="publish:no"),
        ]])
        # Telegram caption limiti 1024 — taşarsa metni ayrı mesajda gönder
        send_caption = f"{full_caption}\n\n— Yayınlansın mı?"
        if len(send_caption) <= 1024:
            await bot.send_video(
                chat_id,
                video=InputFile(io.BytesIO(video_bytes), filename="melisa_reels.mp4"),
                caption=send_caption,
                reply_markup=approve_kb,
            )
        else:
            await bot.send_video(
                chat_id,
                video=InputFile(io.BytesIO(video_bytes), filename="melisa_reels.mp4"),
            )
            await bot.send_message(chat_id, full_caption)
            await bot.send_message(chat_id, "Yayınlansın mı?", reply_markup=approve_kb)

    except PipelineError as e:
        await _handle_pipeline_error(progress, e)
        await notify_admin(bot, f"Üretim Hatası (Chat: {chat_id})\nAşama: {e.stage}\nHata: {e}")
    except Exception as e:
        log.error("Beklenmeyen üretim hatası", exc_info=True)
        await tracker.force_msg(f"❌ Beklenmeyen bir hata oluştu: {e}")
        import traceback
        tb = traceback.format_exc()
        await notify_admin(bot, f"Beklenmeyen Üretim Hatası (Chat: {chat_id})\n```python\n{tb[-2000:]}\n```")
    finally:
        # Üretim bitti (başarılı veya hatalı) — persistence'tan temizle
        task_manager.finish(chat_id, context)


# ─────────────────────────────────────
# Pipeline hata yönetimi
# ─────────────────────────────────────
async def _handle_pipeline_error(progress, e: PipelineError) -> None:
    stage_label = {"image": "Görsel", "video": "Video", "merge": "Birleştirme"}.get(e.stage, "Üretim")
    msg = f"❌ *{stage_label}* aşamasında hata oluştu.\n\n{e}"
    if e.stage in ("image", "video"):
        msg += "\n\nℹ️ Kie AI kredileri başarısız üretimde *iade edilir*, bütçe kaybı yoktur."
        if e.code == "422":
            msg += "\nNot: 422 genellikle model kaynaklı geçici bir hatadır, yeniden deneyebilirsin."
    log.error(f"Pipeline hatası [{e.stage}]: {e}")
    await progress(msg)
