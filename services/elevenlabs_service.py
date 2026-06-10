"""
ElevenLabs TTS Servisi
"""

import os
import aiohttp
import logging
from typing import Optional

logger = logging.getLogger(__name__)

class ElevenLabsService:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.elevenlabs.io/v1"
        self.headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json"
        }
        
        # Varsayılan ses: Rachel veya uygun bir Türkçe ses
        self.default_voice_id = "CwhRBWXzGAHq8TQ4Fs17" # Roger - just as a fallback
        # İdeal Türkçe kadın sesi için ID'yi bulduğumuzda güncelleriz.
        
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
