from __future__ import annotations

"""
AI Influencer — Fail-Fast Config
=================================
Boot anında gerekli ENV değişkenlerini doğrular. Eksikse uygulama anında çöker.

Sabit karakter:
- REFERENCE_IMAGE_URL: Nano Banana Pro'ya "bu kişiyi koru" referansı (public URL).
- CHARACTER_DETAILS: senaryo agent'ına verilen karakter/kanal konsepti.
  Önce CHARACTER_DETAILS env'ine, yoksa prompts/karakter_konsept.txt dosyasına bakar.
"""

import os
import sys

try:
    # Lokal geliştirmede .env'i yükle. Railway env'leri doğrudan enjekte ettiği için
    # orada .env olmasa da sorun olmaz (load_dotenv sessizce geçer).
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Kullanıcının sağladığı varsayılan karakter referans görseli (ImgBB public URL).
DEFAULT_REFERENCE_IMAGE_URL = "https://i.ibb.co/bjcz7Ybv/Model-Referans-Sayfa-1.png"


def _load_character_details() -> str:
    """CHARACTER_DETAILS env'i veya prompts/karakter_konsept.txt'ten konsepti yükler."""
    env_val = os.environ.get("CHARACTER_DETAILS", "").strip()
    if env_val:
        return env_val
    path = os.path.join(_BASE_DIR, "prompts", "karakter_konsept.txt")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            txt = f.read().strip()
        if txt and not txt.startswith("<"):  # placeholder doldurulmamışsa atla
            return txt
    return ""


class Config:
    def __init__(self):
        # ── Ortam Modu ──
        self.ENV = os.environ.get("ENV", "development").lower()

        # ── Telegram ──
        self.TELEGRAM_BOT_TOKEN = self._require_env("TELEGRAM_BOT_TOKEN")
        # Opsiyonel erişim kısıtı: set edilirse sadece bu kullanıcı(lar) botu kullanabilir.
        admin = os.environ.get("ADMIN_CHAT_ID", "").strip()
        self.ADMIN_CHAT_ID = int(admin) if admin.lstrip("-").isdigit() else None
        self.ALLOWED_USER_IDS = [self.ADMIN_CHAT_ID] if self.ADMIN_CHAT_ID else []

        # ── OpenAI (Senaryo Agent) ──
        self.OPENAI_API_KEY = self._require_env("OPENAI_API_KEY")
        # n8n'de senaryo için gpt-5.1 kullanılıyordu.
        self.OPENAI_SCENARIO_MODEL = os.environ.get("OPENAI_SCENARIO_MODEL", "gpt-5.1")

        # ── Kie AI (Nano Banana Pro görsel + Veo 3.1 / Kling 3.0 video) ──
        self.KIE_API_KEY = self._require_env("KIE_API_KEY")
        self.KIE_BASE_URL = os.environ.get("KIE_BASE_URL", "https://api.kie.ai/api/v1/")

        # ── Video Motoru (veo | kling) ──
        # veo   : Veo 3.1 image-to-video, kendi sesiyle (tek servis, stabil) — varsayılan.
        # kling : Kling 3.0 sessiz video + ElevenLabs Türkçe ses + Replicate lip-sync.
        self.VIDEO_ENGINE = os.environ.get("VIDEO_ENGINE", "veo").strip().lower()
        if self.VIDEO_ENGINE not in ("veo", "kling"):
            raise EnvironmentError(
                f"CRITICAL STARTUP FAILURE: VIDEO_ENGINE '{self.VIDEO_ENGINE}' geçersiz. "
                "'veo' veya 'kling' olmalı."
            )

        # ── Replicate (concat + kling motorunda lip-sync) ──
        self.REPLICATE_API_TOKEN = self._require_env("REPLICATE_API_TOKEN")

        # ── ElevenLabs (sadece kling motorunda zorunlu — dış Türkçe seslendirme) ──
        self.ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        self.ELEVENLABS_VOICE_ID = os.environ.get(
            "ELEVENLABS_VOICE_ID", "CwhRBWXzGAHq8TQ4Fs17"
        ).strip()
        if self.VIDEO_ENGINE == "kling" and not self.ELEVENLABS_API_KEY:
            raise EnvironmentError(
                "CRITICAL STARTUP FAILURE: VIDEO_ENGINE=kling için ELEVENLABS_API_KEY zorunlu. "
                ".env'e ekleyin ya da VIDEO_ENGINE=veo kullanın."
            )

        # ── ImgBB (opsiyonel — lokal görseli public URL'e çevirmek için) ──
        self.IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY", "")

        # ── Sabit Karakter ──
        self.REFERENCE_IMAGE_URL = os.environ.get(
            "REFERENCE_IMAGE_URL", DEFAULT_REFERENCE_IMAGE_URL
        ).strip()
        self.CHARACTER_DETAILS = _load_character_details()
        if not self.CHARACTER_DETAILS:
            raise EnvironmentError(
                "CRITICAL STARTUP FAILURE: Karakter konsepti tanımlı değil. "
                "CHARACTER_DETAILS env'ini doldurun VEYA prompts/karakter_konsept.txt dosyasına "
                "karakterin (kim, hangi dünya, kişilik, kıyafet) konseptini yazın."
            )

        # ── Üretim Ayarları ──
        self.ASPECT_RATIO = os.environ.get("ASPECT_RATIO", "9:16")
        self.IMAGE_RESOLUTION = os.environ.get("IMAGE_RESOLUTION", "1K")
        self.MIN_SCENES = int(os.environ.get("MIN_SCENES", "2"))
        self.MAX_SCENES = int(os.environ.get("MAX_SCENES", "4"))
        # Girdi bu kelime sayısına ulaşırsa "kendi metin" (script) modu sayılır:
        # metin birebir sahnelere bölünüp aynen seslendirilir.
        self.SCRIPT_MODE_MIN_WORDS = int(os.environ.get("SCRIPT_MODE_MIN_WORDS", "25"))
        self.OPTIMIZE_PROMPT_CONSISTENCY = os.environ.get("OPTIMIZE_PROMPT_CONSISTENCY", "true").lower() == "true"
        self.DEFAULT_BGM_URL = os.environ.get(
            "DEFAULT_BGM_URL", 
            "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"
        )

        # ── Upload-Post (opsiyonel — sosyal medya yayını) ──
        self.UPLOAD_POST_API_KEY = os.environ.get("UPLOAD_POST_API_KEY", "")
        self.UPLOAD_POST_PROFILE = os.environ.get("UPLOAD_POST_PROFILE", "")
        platforms = os.environ.get("PUBLISH_PLATFORMS", "tiktok,instagram,youtube")
        self.PUBLISH_PLATFORMS = [p.strip() for p in platforms.split(",") if p.strip()]
        self.PUBLISH_ENABLED = bool(self.UPLOAD_POST_API_KEY and self.UPLOAD_POST_PROFILE)

    def _require_env(self, key: str) -> str:
        val = os.environ.get(key)
        if not val:
            raise EnvironmentError(
                f"CRITICAL STARTUP FAILURE: Gerekli ortam değişkeni '{key}' bulunamadı! "
                f".env dosyanıza ekleyin (örnek için .env.example)."
            )
        return val


try:
    settings = Config()
except EnvironmentError as e:
    print(f"BOOT ERROR: {e}")
    sys.exit(1)
