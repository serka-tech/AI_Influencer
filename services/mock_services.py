from __future__ import annotations

"""
Mock Services — Test Modu için Sahte Servisler
===============================================
ENV=test olduğunda gerçek API çağrıları yapmak yerine
sabit bekleme süreleri ve sahte URL'ler/veriler dönen sınıflar.
Geliştirme hızını artırır ve maliyeti sıfırlar.
"""

import asyncio
from logger import get_logger

log = get_logger("mock_services")

# Sabit test çıktıları
MOCK_IMAGE_URL = "https://i.ibb.co/bjcz7Ybv/Model-Referans-Sayfa-1.png"
MOCK_VIDEO_URL = "https://www.w3schools.com/html/mov_bbb.mp4"
MOCK_AUDIO_BYTES = b"mock_audio_bytes_data"


class MockKieAIService:
    def __init__(self, api_key: str, base_url: str = ""):
        log.info("MockKieAIService başlatıldı")

    def create_image(self, *args, **kwargs) -> str:
        return "mock_image_task_123"

    def create_veo_video(self, *args, **kwargs) -> str:
        return "mock_veo_task_123"

    def create_kling_task(self, *args, **kwargs) -> str:
        return "mock_kling_task_123"

    async def async_poll_task(self, task_id: str, callback=None) -> dict:
        await asyncio.sleep(2)
        if callback: callback(1, "success")
        return {"status": "success", "urls": [MOCK_IMAGE_URL]}

    async def async_poll_veo_task(self, task_id: str, callback=None) -> dict:
        await asyncio.sleep(3)
        if callback: callback(1, 1)
        return {"status": "success", "urls": [MOCK_VIDEO_URL]}

    def upload_file_from_url(self, file_url: str, *args, **kwargs) -> str:
        return file_url

    def upload_files_from_urls(self, file_urls: list[str]) -> list[str]:
        return file_urls

    def get_credit_balance(self) -> dict:
        return {"data": 999999}


class MockReplicateService:
    def __init__(self, api_token: str):
        log.info("MockReplicateService başlatıldı")

    async def run_concat(self, *args, **kwargs) -> str:
        await asyncio.sleep(2)
        return MOCK_VIDEO_URL

    async def run_lipsync(self, *args, **kwargs) -> str:
        await asyncio.sleep(2)
        return MOCK_VIDEO_URL


class MockElevenLabsService:
    def __init__(self, api_key: str, voice_id: str | None = None):
        log.info("MockElevenLabsService başlatıldı")
        self.default_voice_id = voice_id or "mock"

    async def generate_audio(self, *args, **kwargs) -> bytes:
        await asyncio.sleep(1)
        return MOCK_AUDIO_BYTES


class MockUploadPostService:
    def __init__(self, api_key: str, profile_name: str):
        log.info("MockUploadPostService başlatıldı")

    def upload_video(self, *args, **kwargs) -> dict:
        import time
        time.sleep(1)
        return {"request_id": "mock-req-123", "status": "success"}
