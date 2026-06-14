from __future__ import annotations

"""
Bot Services — Merkezi Servis Başlatma
=======================================
Tüm handler modülleri servis instance'larını buradan import eder.
Boot anında bir kez çalışır; singleton pattern.
"""

from config import settings
from logger import get_logger
from services.kie_api import KieAIService
from services.replicate_service import ReplicateService
from services.openai_service import OpenAIService
from services.upload_post_service import UploadPostService
from services.elevenlabs_service import ElevenLabsService
from core.scenario_engine import ScenarioEngine
from core.topic_resolver import TopicResolver
from core.caption_generator import CaptionGenerator
from core.production_pipeline import ProductionPipeline

log = get_logger("bot_services")

# ── Servisler (boot anında bir kez) ──
if settings.ENV == "test":
    log.info("🧪 TEST MODU AKTİF — Mock servisler kullanılıyor")
    from services.mock_services import (
        MockKieAIService,
        MockReplicateService,
        MockElevenLabsService,
        MockUploadPostService,
    )
    kie = MockKieAIService(settings.KIE_API_KEY, base_url=settings.KIE_BASE_URL)
    replicate_svc = MockReplicateService(settings.REPLICATE_API_TOKEN)
    openai_svc = OpenAIService(settings.OPENAI_API_KEY, model=settings.OPENAI_SCENARIO_MODEL)
    elevenlabs_svc = (
        MockElevenLabsService(settings.ELEVENLABS_API_KEY, voice_id=settings.ELEVENLABS_VOICE_ID)
        if settings.VIDEO_ENGINE == "kling" else None
    )
    upload_post = (
        MockUploadPostService(settings.UPLOAD_POST_API_KEY, profile_name=settings.UPLOAD_POST_PROFILE)
        if settings.PUBLISH_ENABLED else None
    )
else:
    kie = KieAIService(settings.KIE_API_KEY, base_url=settings.KIE_BASE_URL)
    replicate_svc = ReplicateService(settings.REPLICATE_API_TOKEN)
    openai_svc = OpenAIService(settings.OPENAI_API_KEY, model=settings.OPENAI_SCENARIO_MODEL)
    elevenlabs_svc = (
        ElevenLabsService(settings.ELEVENLABS_API_KEY, voice_id=settings.ELEVENLABS_VOICE_ID)
        if settings.VIDEO_ENGINE == "kling" else None
    )
    upload_post = (
        UploadPostService(settings.UPLOAD_POST_API_KEY, profile_name=settings.UPLOAD_POST_PROFILE)
        if settings.PUBLISH_ENABLED else None
    )

scenario_engine = ScenarioEngine(openai_svc)
topic_resolver = TopicResolver(openai_svc)
caption_generator = CaptionGenerator(openai_svc)
pipeline = ProductionPipeline(
    kie=kie,
    replicate=replicate_svc,
    elevenlabs=elevenlabs_svc,
    engine=settings.VIDEO_ENGINE,
    reference_image_url=settings.REFERENCE_IMAGE_URL,
    aspect_ratio=settings.ASPECT_RATIO,
    resolution=settings.IMAGE_RESOLUTION,
)
