from __future__ import annotations

"""
Scenario Handlers — Senaryo Üretim, Onay ve Düzenleme
=======================================================
Senaryoyu (yeniden) üretir, Telegram'da gösterir, onay butonlarını sunar
ve kullanıcının düzenleme/yeniden üretme isteklerini yönetir.
"""

import asyncio
import json

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import ContextTypes, ConversationHandler

from config import settings
from logger import get_logger
from core.scenario_engine import ScenarioError
from core.topic_resolver import segment_script
from core import place_research
from handlers.shared import (
    SCENARIO_REVIEW,
    SCENARIO_EDIT,
    send_long,
)
from bot_services import kie, openai_svc, scenario_engine, topic_resolver

log = get_logger("handlers.scenario")


# ─────────────────────────────────────
# Senaryo formatlama (Telegram'da gösterim)
# ─────────────────────────────────────
def _format_scenario(
    scenario: dict,
    topic: str,
    language: str,
    scene_count: int,
    venue_mode: bool = False,
    summary_tr: list[dict] | None = None,
    places: list[dict] | None = None,
) -> str:
    parts = [
        "🎬 *Konuşma metni hazır*",
        f"Konu: {topic}",
        f"Dil: {language} · {scene_count} sahne",
    ]
    if venue_mode:
        parts.append("📍 Mod: Mekan tanıtımı (mekan videosu kayıtlı)")
    if places:
        names = ", ".join(p["name"] for p in places)
        parts.append(f"🗺️ Keşif: {names}")
    parts.append("")
    parts.append("Melisa'nın sahne sahne ne söyleyeceği aşağıda 👇")
    parts.append("")
    for i, scene in enumerate(scenario["scenes"], 1):
        tr = summary_tr[i - 1] if summary_tr and i - 1 < len(summary_tr) else None
        konusma = (tr or {}).get("konusma", "").strip() if tr else ""
        ortam = (tr or {}).get("sahne", "").strip() if tr else ""
        place_name = places[i - 1]["name"] if places and i - 1 < len(places) else None
        if konusma:
            header = f"🎞️ *Sahne {i}*"
            if place_name:
                header += f" — 📍 {place_name}"
            parts.append(header)
            parts.append(f"💬 {konusma}")
            if ortam:
                parts.append(f"_({ortam})_")
        else:
            # Türkçe özet üretilemediyse en azından sahneyi belirt
            parts.append(f"🎞️ *Sahne {i}* — (Türkçe konuşma çıkarılamadı)")
        parts.append("")
    parts.append(f"📝 Paylaşım açıklaması: {scenario['video_caption']}")
    return "\n".join(parts)


# ─────────────────────────────────────
# Keşif/araştırma modu (otomatik)
# ─────────────────────────────────────
async def _maybe_research_places(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Konu liste/keşif tipiyse (örn. 'Mersin'de koylar') gerçek mekanları internetten araştırır,
    her mekan için referans görsel bulur ve context.user_data['places'] içine yazar.
    Her durumda 'places' anahtarını set eder (boş liste = keşif değil) ki tekrar çalışmasın.
    """
    bot = context.bot
    topic = context.user_data.get("topic", "")
    scene_count = context.user_data["scenes"]

    cls = await asyncio.to_thread(place_research.classify_discovery, topic, openai_svc)
    if not cls.get("is_discovery"):
        context.user_data["places"] = []
        return

    await bot.send_message(chat_id, "🔎 İnternetten gerçek mekanlar araştırılıyor...")
    places = await asyncio.to_thread(
        place_research.research_places, cls.get("search_query", topic), openai_svc, scene_count
    )
    if not places:
        await bot.send_message(
            chat_id,
            "ℹ️ İnternetten net mekan bulamadım; senaryoyu genel olarak hazırlayacağım.",
        )
        context.user_data["places"] = []
        return

    # Sahne sayısı bulunan mekan sayısından fazlaysa eşitle (her sahne = bir mekan)
    if len(places) < scene_count:
        context.user_data["scenes"] = len(places)
        await bot.send_message(
            chat_id,
            f"ℹ️ {len(places)} mekan bulundu; sahne sayısını {len(places)}'e göre ayarladım.",
        )

    # Referans görselleri Kie dosya sunucusuna yükle (Nano Banana için daha güvenilir)
    found_imgs = 0
    for p in places:
        ref = p.get("ref_image_url")
        if not ref:
            continue
        try:
            p["ref_image_url"] = await asyncio.to_thread(
                kie.upload_file_from_url, ref, "place_ref.jpg", "images/place-refs"
            )
            found_imgs += 1
        except Exception:
            log.warning(f"Mekan görseli Kie'ye yüklenemedi, orijinal URL kullanılacak: {p['name']}", exc_info=True)
            found_imgs += 1  # orijinali yine de kullanacağız

    context.user_data["places"] = places
    names = "\n".join(f"{i}. {p['name']}" for i, p in enumerate(places, 1))
    await bot.send_message(
        chat_id,
        f"✅ Bulunan mekanlar:\n{names}\n\n"
        f"📸 {found_imgs}/{len(places)} mekan için referans görsel bulundu "
        "(Melisa o mekandaymış gibi üretilecek).",
    )


# ─────────────────────────────────────
# Sahne sayısını otomatik belirle
# ─────────────────────────────────────
async def _determine_scene_count(
    chat_id: int, context: ContextTypes.DEFAULT_TYPE, topic: str, input_mode: str
) -> None:
    """
    Sahne sayısını içeriğe göre otomatik belirler ve context.user_data['scenes']'e yazar.

    - script modu: kullanıcının metnini birebir sahnelere böler (scene_lines) ve sahne
      sayısını segment sayısına eşitler. Çok kısa metin (< MIN_SCENES) konu moduna düşülür.
    - konu modu: konunun zenginliğine göre LLM ile sahne sayısı önerir. (Keşif araştırması
      daha sonra bu sayıyı düşürebilir.)
    """
    bot = context.bot
    min_s, max_s = settings.MIN_SCENES, settings.MAX_SCENES

    if input_mode == "script":
        raw_input = context.user_data.get("raw_input", "")
        # Sahne başına kelime bütçesi: Veo ~14, diğer motorlar ~10
        max_words = 14 if settings.VIDEO_ENGINE == "veo" else 10
        lines = segment_script(raw_input, max_words, max_s)
        if len(lines) >= min_s:
            context.user_data["scene_lines"] = lines
            context.user_data["scenes"] = len(lines)
            await bot.send_message(
                chat_id,
                f"📝 Metnini {len(lines)} sahneye böldüm; cümlelerin birebir seslendirilecek.",
            )
            return
        # Çok kısa metin → konu gibi davranıp genişletelim
        context.user_data["input_mode"] = "topic"

    # Konu modu: konuya göre LLM önerisi
    n = await asyncio.to_thread(
        scenario_engine.recommend_scene_count,
        topic, context.user_data["language"], min_s, max_s,
    )
    context.user_data["scenes"] = n


# ─────────────────────────────────────
# Senaryo üretimi + gösterim + onay
# ─────────────────────────────────────
async def build_scenario(chat_id: int, context: ContextTypes.DEFAULT_TYPE, feedback: str | None = None) -> int:
    """Senaryoyu (yeniden) üretir, Telegram'da gösterir ve onay butonlarını sunar."""
    bot = context.bot
    language = context.user_data["language"]
    raw_input = context.user_data.get("raw_input", "")
    input_mode = context.user_data.get("input_mode", "topic")

    try:
        # Konu bir kez çözülür, sonraki revizyonlarda yeniden kullanılır
        topic = context.user_data.get("topic")
        if topic is None:
            await bot.send_message(chat_id, "🔎 Konu hazırlanıyor...")
            topic, source_note = await asyncio.to_thread(
                topic_resolver.resolve, raw_input, settings.CHARACTER_DETAILS, language
            )
            context.user_data["topic"] = topic
            if source_note:
                await bot.send_message(chat_id, source_note)

        # Sahne sayısı (ve script modunda birebir konuşma cümleleri) bir kez belirlenir;
        # revizyon/yeniden üretimde korunur.
        if "scenes" not in context.user_data:
            await _determine_scene_count(chat_id, context, topic, input_mode)
        # Çok kısa metin script modundan konu moduna düşmüş olabilir
        input_mode = context.user_data.get("input_mode", "topic")

        # Keşif modu: liste tipi konularda (örn. "Mersin'de koylar") gerçek mekanları araştır.
        # Bir kez yapılır, revizyonlarda yeniden kullanılır. Mekan ve script modunda devre dışı.
        if (
            input_mode != "script"
            and "places" not in context.user_data
            and not context.user_data.get("venue_mode")
        ):
            await _maybe_research_places(chat_id, context)

        places = context.user_data.get("places") or []
        scene_count = context.user_data["scenes"]  # araştırma sahne sayısını düşürmüş olabilir
        scene_lines = context.user_data.get("scene_lines")

        await bot.send_message(
            chat_id, "✏️ Senaryo güncelleniyor..." if feedback else "✍️ Senaryo yazılıyor..."
        )

        # Konuyu modlara göre yönlendir
        scenario_topic = topic
        place_assignments = None
        if context.user_data.get("venue_mode"):
            scenario_topic = (
                f"{topic}\n\n"
                "NOT: Bu bir MEKAN TANITIMI videosudur. Karakter bu mekanı ziyaret etmiş bir "
                "influencer gibi, mekanın içinde/önündeymiş hissi veren ortamlarda konuşsun; "
                "mekanın adını ve öne çıkan özelliklerini doğal, samimi bir dille anlatsın ve "
                "izleyiciyi mekana gelmeye davet eden bir kapanış yapsın."
            )
        elif places:
            place_assignments = [p["name"] for p in places]

        previous = context.user_data.get("scenario") if feedback else None
        scenario = await asyncio.to_thread(
            scenario_engine.generate,
            settings.CHARACTER_DETAILS,
            scenario_topic,
            scene_count,
            language,
            3,
            feedback,
            previous,
            place_assignments,
            optimize_consistency=settings.OPTIMIZE_PROMPT_CONSISTENCY,
            scene_lines=scene_lines,
        )
        # Keşif modunda her sahneye o mekanın referans görselini bağla (Nano Banana 2. referans)
        if places:
            for i, scene in enumerate(scenario.get("scenes", [])):
                if i < len(places) and places[i].get("ref_image_url"):
                    scene["reference_image_url"] = places[i]["ref_image_url"]
        context.user_data["scenario"] = scenario

        # Üretim promptları İngilizce; kullanıcıya konuşma metnini Türkçe göstermek için özet üret
        await bot.send_message(chat_id, "🔁 Konuşma metni Türkçe hazırlanıyor...")
        summary_tr = await asyncio.to_thread(scenario_engine.summarize_tr, scenario)

        await send_long(
            bot, chat_id,
            _format_scenario(
                scenario, topic, language, scene_count,
                context.user_data.get("venue_mode", False),
                summary_tr,
                places,
            ),
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Onayla", callback_data="scenario:approve")],
            [
                InlineKeyboardButton("✏️ Düzenle", callback_data="scenario:edit"),
                InlineKeyboardButton("🔄 Yeniden üret", callback_data="scenario:regen"),
            ],
        ])
        await bot.send_message(
            chat_id,
            "Konuşma metnini onaylıyor musun?\n"
            "• ✅ *Onayla* — maliyeti göstereyim, sonra üretelim\n"
            "• ✏️ *Düzenle* — konuşmada neyi değiştireceğimi yaz\n"
            "• 🔄 *Yeniden üret* — baştan farklı bir konuşma metni",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return SCENARIO_REVIEW

    except ScenarioError as e:
        log.error(f"Senaryo hatası: {e}", exc_info=True)
        await bot.send_message(chat_id, f"❌ Senaryo üretilemedi: {e}\n/baslat ile tekrar dene.")
        return ConversationHandler.END
    except Exception as e:
        log.error("Senaryo aşamasında beklenmeyen hata", exc_info=True)
        await bot.send_message(chat_id, f"❌ Beklenmeyen bir hata oluştu: {e}\n/baslat ile tekrar dene.")
        return ConversationHandler.END


# ─────────────────────────────────────
# Senaryo onay/düzenleme/yeniden üretim
# ─────────────────────────────────────
async def on_scenario_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    decision = query.data.split(":", 1)[1]
    chat_id = query.message.chat_id

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if decision == "approve":
        from handlers.cost import show_cost
        return await show_cost(chat_id, context)

    if decision == "edit":
        await context.bot.send_message(
            chat_id,
            "✏️ Konuşmada neyi değiştireyim? Serbestçe yaz.\n"
            "Örn: _2. sahnede şunu söylesin: ..._, _konuşma daha enerjik olsun_, "
            "_1. sahnedeki cümle daha kısa olsun_.",
            parse_mode="Markdown",
        )
        return SCENARIO_EDIT

    if decision == "regen":
        # Geri bildirimsiz: baştan farklı bir senaryo üret
        return await build_scenario(chat_id, context, feedback=None)

    return SCENARIO_REVIEW


async def on_scenario_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    feedback = update.message.text.strip()
    return await build_scenario(update.message.chat_id, context, feedback=feedback)
