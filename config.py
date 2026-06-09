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

        # ── Kie AI (Nano Banana Pro + Veo 3.1) ──
        self.KIE_API_KEY = self._require_env("KIE_API_KEY")
        self.KIE_BASE_URL = os.environ.get("KIE_BASE_URL", "https://api.kie.ai/api/v1/")

        # ── Replicate (Sahne birleştirme — concat) ──
        self.REPLICATE_API_TOKEN = self._require_env("REPLICATE_API_TOKEN")

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
        self.MAX_SCENES = int(os.environ.get("MAX_SCENES", "6"))

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
