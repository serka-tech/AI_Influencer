"""
ElevenLabs TTS Servisi
"""

from __future__ import annotations

import os
import aiohttp
import logging
from typing import Optional

logger = logging.getLogger(__name__)

class ElevenLabsService:
    def __init__(self, api_key: str, voice_id: str | None = None):
        self.api_key = api_key
        self.base_url = "https://api.elevenlabs.io/v1"
        self.headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json"
        }

        # Varsayılan ses config'den (ELEVENLABS_VOICE_ID) gelir; verilmezse fallback ID.
        self.default_voice_id = voice_id or "CwhRBWXzGAHq8TQ4Fs17"
        
    async def generate_audio(self, text: str, voice_id: Optional[str] = None) -> bytes:
        """Metni sese çevirir ve MP3 byte dizisi döner."""
        vid = voice_id or self.default_voice_id
        url = f"{self.base_url}/text-to-speech/{vid}"
        
        payload = {
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75
            }
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=self.headers) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise RuntimeError(f"ElevenLabs API hatası ({response.status}): {error_text}")
                
                audio_bytes = await response.read()
                logger.info(f"ElevenLabs ses üretildi. Boyut: {len(audio_bytes)} byte")
                return audio_bytes
