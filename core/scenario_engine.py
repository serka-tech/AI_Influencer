from __future__ import annotations

"""
Scenario Engine — Çok Sahneli Veo 3.1 Senaryo Üretimi
=======================================================
n8n "Senaryo Agent Veo 3.1" + "Formatlayıcı" node'larının kod karşılığı.

GPT'ye sabit karakter konsepti + kullanıcı girdilerini (konu, dil, sahne sayısı) verir;
her sahne için text_to_image_prompt + image_to_video_prompt ve tek bir video_caption
içeren JSON üretir. JSON parse + sahne sayısı doğrulaması + auto-fix retry yapar.
"""

import json
import os

from logger import get_logger
from services.openai_service import OpenAIService

log = get_logger("scenario_engine")

_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")


def _load_prompt(filename: str) -> str:
    path = os.path.join(_PROMPTS_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class ScenarioError(Exception):
    """Senaryo üretimi/doğrulaması başarısız olduğunda fırlatılır."""


class ScenarioEngine:
    """Çok sahneli influencer senaryosu üretir ve doğrular."""

    def __init__(self, openai_service: OpenAIService):
        self.openai = openai_service
        self.system_prompt = _load_prompt("senaryo_system_prompt.txt")
        self.user_template = _load_prompt("senaryo_user_template.txt")

    def _build_user_prompt(
        self,
        character_details: str,
        video_topic: str,
        scene_count: int,
        speech_language: str,
    ) -> str:
        return (
            self.user_template
            .replace("__CHARACTER_DETAILS__", character_details.strip())
            .replace("__VIDEO_TOPIC__", video_topic.strip())
            .replace("__SCENE_COUNT__", str(scene_count))
            .replace("__SPEECH_LANGUAGE__", speech_language.strip())
        )

    def generate(
        self,
        character_details: str,
        video_topic: str,
        scene_count: int,
        speech_language: str,
        max_attempts: int = 3,
        feedback: str | None = None,
        previous: dict | None = None,
        place_assignments: list[str] | None = None,
    ) -> dict:
        """
        Senaryo üretir.

        feedback + previous verilirse, önceki senaryoyu kullanıcı geri bildirimine
        göre revize eder (Telegram'da "✏️ Düzenle" akışı için). Aksi halde sıfırdan üretir.

        Returns:
            {"scenes": [{"text_to_image_prompt": ..., "image_to_video_prompt": ...}, ...],
             "video_caption": "..."}

        Raises:
            ScenarioError: max_attempts sonunda geçerli senaryo üretilemezse.
        """
        user_prompt = self._build_user_prompt(
            character_details, video_topic, scene_count, speech_language
        )
        # Keşif modu: her sahne sırayla belirli bir gerçek mekana ayrılır
        if place_assignments:
            lines = "\n".join(
                f"- Sahne {i}: {name}" for i, name in enumerate(place_assignments, 1)
            )
            user_prompt += (
                "\n\nÖNEMLİ — MEKAN ATAMASI (keşif videosu):\n"
                "Bu video, gerçek yerleri tek tek tanıtan bir liste/keşif videosudur. "
                "Her sahne SIRAYLA aşağıdaki yere ayrılmıştır ve karakter o sahnede o yerin "
                "İSMİNİ Türkçe konuşmasında AÇIKÇA söylemelidir (doğal bir şekilde, tırnaksız):\n"
                f"{lines}\n"
                "Karakter her sahnede ilgili yerde/önünde gibi görünmeli; ortamı o yerin "
                "tipik atmosferine uygun tarif et. Son sahnede kısa bir kapanış/çağrı yap."
            )

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # Revizyon: önceki senaryoyu ve kullanıcının istediği değişikliği modele ilet
        if feedback and previous:
            messages.append({
                "role": "assistant",
                "content": json.dumps(previous, ensure_ascii=False),
            })
            messages.append({
                "role": "user",
                "content": (
                    "Yukarıdaki senaryoyu temel al. Kullanıcı şu değişiklikleri istiyor:\n"
                    f"{feedback.strip()}\n\n"
                    "Sadece istenen değişiklikleri uygula, gerisini olabildiğince koru. "
                    f"Yine TAM OLARAK {scene_count} sahne içeren, sadece 'scenes' ve "
                    "'video_caption' anahtarlı geçerli JSON üret."
                ),
            })

        last_error = None
        for attempt in range(1, max_attempts + 1):
            try:
                result = self.openai.chat_json(messages, max_tokens=4000)
                self._validate(result, scene_count)
                log.info(
                    f"Senaryo üretildi: {len(result['scenes'])} sahne, "
                    f"caption={len(result.get('video_caption', ''))} kr (deneme {attempt})"
                )
                return result
            except ScenarioError as e:
                last_error = e
                log.warning(f"Senaryo doğrulama hatası (deneme {attempt}/{max_attempts}): {e}")
                # Auto-fix: hatayı modele geri besle
                messages.append({
                    "role": "user",
                    "content": (
                        f"Önceki çıktı şu kuralı ihlal etti: {e}. "
                        f"Lütfen TAM OLARAK {scene_count} sahne içeren, "
                        f"sadece 'scenes' ve 'video_caption' anahtarlı geçerli JSON üret."
                    ),
                })
            except Exception as e:
                last_error = e
                log.error(f"Senaryo üretim hatası (deneme {attempt}/{max_attempts}): {e}", exc_info=True)

        raise ScenarioError(f"Senaryo {max_attempts} denemede üretilemedi: {last_error}")

    def summarize_tr(self, scenario: dict) -> list[dict]:
        """
        Üretim promptları İngilizcedir; bu metot kullanıcıya Telegram'da göstermek için
        her sahnenin TÜRKÇE özetini üretir.

        Returns:
            [{"sahne": "<ortam/aksiyon özeti>", "konusma": "<karakterin söylediği söz>"}, ...]
            Sahne sayısı kadar eleman. Hata olursa boş liste döner (çağıran İngilizce'ye düşer).
        """
        scenes = scenario.get("scenes", [])
        n = len(scenes)
        if n == 0:
            return []

        blocks = []
        for i, s in enumerate(scenes, 1):
            blocks.append(
                f"Sahne {i}:\n"
                f"- görsel: {s.get('text_to_image_prompt', '')}\n"
                f"- video+ses: {s.get('image_to_video_prompt', '')}"
            )
        joined = "\n\n".join(blocks)

        prompt = (
            "Aşağıda bir kısa video senaryosunun teknik (İngilizce) sahne promptları var. "
            "Her sahne için TÜRKÇE, kısa ve anlaşılır bir özet çıkar:\n"
            "- 'sahne': o sahnede ne görünüyor (ortam, aksiyon, ruh hali) — 1 cümle.\n"
            "- 'konusma': karakterin o sahnede Türkçe ne söylediği — doğal, akıcı Türkçe tek cümle "
            "(promptta dolaylı anlatılmış olabilir, sen düz konuşma cümlesine çevir).\n\n"
            f"TAM OLARAK {n} sahne var. Şu JSON yapısında döndür:\n"
            '{"sahneler": [{"sahne": "...", "konusma": "..."}, ...]}\n'
            "Başka anahtar ekleme, JSON dışına yazı yazma.\n\n"
            f"{joined}"
        )

        try:
            result = self.openai.chat_json(
                [{"role": "user", "content": prompt}], max_tokens=2000
            )
            items = result.get("sahneler") if isinstance(result, dict) else None
            if not isinstance(items, list) or len(items) != n:
                log.warning(f"Türkçe özet sahne sayısı uyuşmadı: beklenen {n}, gelen {items}")
                return []
            normalized = []
            for it in items:
                if isinstance(it, dict):
                    normalized.append({
                        "sahne": str(it.get("sahne", "")).strip(),
                        "konusma": str(it.get("konusma", "")).strip(),
                    })
                else:
                    normalized.append({"sahne": str(it).strip(), "konusma": ""})
            return normalized
        except Exception:
            log.warning("Türkçe özet üretilemedi, İngilizce prompt gösterilecek", exc_info=True)
            return []

    @staticmethod
    def _validate(result: dict, scene_count: int) -> None:
        if not isinstance(result, dict):
            raise ScenarioError("Çıktı bir JSON objesi değil")

        scenes = result.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            raise ScenarioError("'scenes' dizisi yok veya boş")

        if len(scenes) != scene_count:
            raise ScenarioError(
                f"Sahne sayısı uyuşmuyor: beklenen {scene_count}, gelen {len(scenes)}"
            )

        for i, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict):
                raise ScenarioError(f"Sahne {i} obje değil")
            t2i = scene.get("text_to_image_prompt")
            i2v = scene.get("image_to_video_prompt")
            if not isinstance(t2i, str) or not t2i.strip():
                raise ScenarioError(f"Sahne {i}: text_to_image_prompt boş")
            if not isinstance(i2v, str) or not i2v.strip():
                raise ScenarioError(f"Sahne {i}: image_to_video_prompt boş")

        caption = result.get("video_caption")
        if not isinstance(caption, str) or not caption.strip():
            raise ScenarioError("video_caption boş")
