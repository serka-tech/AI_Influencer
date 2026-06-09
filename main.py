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
"""

import asyncio
import io

import requests
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
    PicklePersistence,
)

from config import settings
from logger import get_logger, log_cost
from services.kie_api import KieAIService
from services.replicate_service import ReplicateService
from services.openai_service import OpenAIService
from services.upload_post_service import UploadPostService
from core.scenario_engine import ScenarioEngine, ScenarioError
from core.production_pipeline import ProductionPipeline, PipelineError
from core.topic_resolver import TopicResolver
from core.caption_generator import CaptionGenerator
from core.cost import estimate_cost, format_cost
from core import place_research

log = get_logger("main")

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
) = range(8)

# Her sahne Veo 3.1'de ~8 saniye
SECONDS_PER_SCENE = 8

# ── Servisler (boot anında bir kez) ──
kie = KieAIService(settings.KIE_API_KEY, base_url=settings.KIE_BASE_URL)
replicate_svc = ReplicateService(settings.REPLICATE_API_TOKEN)
openai_svc = OpenAIService(settings.OPENAI_API_KEY, model=settings.OPENAI_SCENARIO_MODEL)
scenario_engine = ScenarioEngine(openai_svc)
topic_resolver = TopicResolver(openai_svc)
caption_generator = CaptionGenerator(openai_svc)
pipeline = ProductionPipeline(
    kie=kie,
    replicate=replicate_svc,
    reference_image_url=settings.REFERENCE_IMAGE_URL,
    aspect_ratio=settings.ASPECT_RATIO,
    resolution=settings.IMAGE_RESOLUTION,
)
upload_post = (
    UploadPostService(settings.UPLOAD_POST_API_KEY, profile_name=settings.UPLOAD_POST_PROFILE)
    if settings.PUBLISH_ENABLED
    else None
)


# ─────────────────────────────────────
# Erişim kontrolü
# ─────────────────────────────────────
def _authorized(update: Update) -> bool:
    if not settings.ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return bool(user and user.id in settings.ALLOWED_USER_IDS)


# ─────────────────────────────────────
# Conversation: form akışı
# ─────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _authorized(update):
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


async def _ask_input(message, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Konu/girdi sorusunu gönderir. Mekan modunda mekan bilgisini ister."""
    if context.user_data.get("venue_mode"):
        await message.reply_text(
            "📝 Mekan hakkında kısa bilgi ver:\n"
            "• Mekanın *adı* ve öne çıkarmak istediğin şeyler\n"
            "  (örn: “Marin Cafe — deniz manzarası, taze deniz ürünleri, gün batımı”)\n"
            "• İstersen mekanın *web/menü linkini* yapıştır\n"
            "• Bilgi yoksa *“yok”* yaz, videoya ve mekana göre ben kurgulayayım",
            parse_mode="Markdown",
        )
    else:
        await message.reply_text(
            "Tanıtmak istediğin bir şey var mı?\n"
            "• 🔗 Mekan/ürün *linki* yapıştır (web sitesi, menü sayfası)\n"
            "• ✍️ Ya da konuyu *kısaca yaz* (örn: “deniz manzaralı yeni bir kafe”)\n"
            "• 🤖 Hiçbir şey yoksa *“yok”* yaz, konuyu ben kendim bulayım",
            parse_mode="Markdown",
        )
    return ASK_INPUT


async def on_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Ham giriş — konu çözümü üretim aşamasında yapılır (LLM/fetch zaman alabilir)
    context.user_data["raw_input"] = update.message.text.strip()
    buttons = [
        InlineKeyboardButton("Türkçe", callback_data="lang:Türkçe"),
        InlineKeyboardButton("English", callback_data="lang:English"),
    ]
    await update.message.reply_text(
        "🗣️ Konuşma dili? (ya da başka bir dil yaz)",
        reply_markup=InlineKeyboardMarkup([buttons]),
    )
    return ASK_LANGUAGE


async def on_language_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["language"] = query.data.split(":", 1)[1]
    await query.edit_message_text(f"🗣️ Dil: {context.user_data['language']}")
    return await _ask_scenes(query.message, context)


async def on_language_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["language"] = update.message.text.strip()
    return await _ask_scenes(update.message, context)


async def _ask_scenes(message, context: ContextTypes.DEFAULT_TYPE) -> int:
    word_count = len(context.user_data.get("raw_input", "").split())
    # Ortalama 15 kelime = ~8 saniye (normal konuşma hızı)
    est_scenes = max(settings.MIN_SCENES, min(settings.MAX_SCENES, (word_count // 15) + 1))

    buttons = [
        InlineKeyboardButton(f"{n} (~{n * SECONDS_PER_SCENE} sn)", callback_data=f"scenes:{n}")
        for n in range(settings.MIN_SCENES, settings.MAX_SCENES + 1)
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    
    # Otomatik butonunu en üste ekle
    rows.insert(0, [InlineKeyboardButton(f"🤖 Metne Göre Otomatik (~{est_scenes} sahne)", callback_data="scenes:auto")])
    
    await message.reply_text(
        f"🎞️ Kaç sahne olsun?\n(Her sahne ~{SECONDS_PER_SCENE} sn; toplam uzunluk buna göre)",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return ASK_SCENES


async def on_scenes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    data = query.data.split(":")[1]
    if data == "auto":
        word_count = len(context.user_data.get("raw_input", "").split())
        scene_count = max(settings.MIN_SCENES, min(settings.MAX_SCENES, (word_count // 15) + 1))
    else:
        scene_count = int(data)
        
    context.user_data["scenes"] = scene_count
    await query.edit_message_text(f"🎞️ {scene_count} sahne seçildi.")

    # Senaryoyu üret ve onaya sun (uzun iş değil, conversation içinde kalır)
    return await _build_scenario(query.message.chat_id, context)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Arka planda üretim sürüyorsa onu da durdur
    task = context.user_data.get("production_task")
    cancelled_production = False
    if task and not task.done():
        task.cancel()
        cancelled_production = True

    context.user_data.clear()
    if cancelled_production:
        await update.message.reply_text(
            "🛑 Üretim durduruldu. /baslat ile yeniden başlayabilirsin.\n"
            "ℹ️ Başlamış Kie işleri varsa başarısız sayılır ve kredi iade edilir."
        )
    else:
        await update.message.reply_text("❌ İptal edildi. /baslat ile yeniden başlayabilirsin.")
    return ConversationHandler.END


# ─────────────────────────────────────
# Senaryo: üretim + gösterim + onay/düzenleme
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


async def _send_long(bot, chat_id: int, text: str, limit: int = 3800) -> None:
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


async def _build_scenario(chat_id: int, context: ContextTypes.DEFAULT_TYPE, feedback: str | None = None) -> int:
    """Senaryoyu (yeniden) üretir, Telegram'da gösterir ve onay butonlarını sunar."""
    bot = context.bot
    language = context.user_data["language"]
    scene_count = context.user_data["scenes"]
    raw_input = context.user_data.get("raw_input", "")

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

        # Keşif modu: liste tipi konularda (örn. "Mersin'de koylar") gerçek mekanları araştır.
        # Bir kez yapılır, revizyonlarda yeniden kullanılır. Mekan modunda devre dışı.
        if "places" not in context.user_data and not context.user_data.get("venue_mode"):
            await _maybe_research_places(chat_id, context)

        places = context.user_data.get("places") or []
        scene_count = context.user_data["scenes"]  # araştırma sahne sayısını düşürmüş olabilir

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

        await _send_long(
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
        return await _show_cost(chat_id, context)

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
        return await _build_scenario(chat_id, context, feedback=None)

    return SCENARIO_REVIEW


async def on_scenario_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    feedback = update.message.text.strip()
    return await _build_scenario(update.message.chat_id, context, feedback=feedback)


# ─────────────────────────────────────
# Maliyet: gösterim + onay
# ─────────────────────────────────────
async def _show_cost(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
    scene_count = context.user_data["scenes"]
    cost = estimate_cost(scene_count)
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
    context.user_data["production_task"] = asyncio.create_task(_run_production(update, context))
    return ConversationHandler.END


# ─────────────────────────────────────
# Üretim pipeline'ı
# ─────────────────────────────────────
async def _run_production(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    bot = context.bot
    language = context.user_data["language"]
    # Senaryo ve konu, onay aşamalarında üretildi ve user_data'ya yazıldı
    topic = context.user_data["topic"]
    scenario = context.user_data["scenario"]

    async def progress(msg: str) -> None:
        try:
            await bot.send_message(chat_id, msg)
        except Exception:
            log.warning("Progress mesajı gönderilemedi", exc_info=True)

    try:
        # Görsel + Video + Birleştirme (senaryo onaylı)
        final_video_url = await pipeline.run(scenario, progress=progress)

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
        video_bytes = await asyncio.to_thread(_download, final_video_url)

        # Maliyeti logla
        scene_count = context.user_data.get("scenes", len(scenario.get("scenes", [])))
        actual_cost = estimate_cost(scene_count)
        try:
            await asyncio.to_thread(log_cost, "AI_Influencer", actual_cost, f"{scene_count} sahne video üretimi")
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
    except Exception as e:
        log.error("Beklenmeyen üretim hatası", exc_info=True)
        await progress(f"❌ Beklenmeyen bir hata oluştu: {e}")


async def _handle_pipeline_error(progress, e: PipelineError) -> None:
    stage_label = {"image": "Görsel", "video": "Video", "merge": "Birleştirme"}.get(e.stage, "Üretim")
    msg = f"❌ *{stage_label}* aşamasında hata oluştu.\n\n{e}"
    if e.stage in ("image", "video"):
        msg += "\n\nℹ️ Kie AI kredileri başarısız üretimde *iade edilir*, bütçe kaybı yoktur."
        if e.code == "422":
            msg += "\nNot: 422 genellikle model kaynaklı geçici bir hatadır, yeniden deneyebilirsin."
    log.error(f"Pipeline hatası [{e.stage}]: {e}")
    await progress(msg)


# ─────────────────────────────────────
# Onay / Yayınlama
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


# ─────────────────────────────────────
# Yardımcılar
# ─────────────────────────────────────
def _download(url: str) -> bytes:
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    return resp.content


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
            ASK_LANGUAGE: [
                CallbackQueryHandler(on_language_button, pattern=r"^lang:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_language_text),
            ],
            ASK_SCENES: [CallbackQueryHandler(on_scenes, pattern=r"^scenes:")],
            SCENARIO_REVIEW: [CallbackQueryHandler(on_scenario_decision, pattern=r"^scenario:")],
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

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
