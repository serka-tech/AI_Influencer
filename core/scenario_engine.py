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
import re

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
        optimize_consistency: bool = False,
        scene_lines: list[str] | None = None,
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
        # Kendi metin (script) modu: konuşma cümleleri kullanıcıdan birebir gelir;
        # sahne sayısı bu cümle listesine eşitlenir.
        if scene_lines:
            scene_count = len(scene_lines)

        user_prompt = self._build_user_prompt(
            character_details, video_topic, scene_count, speech_language
        )

        # Dinamik Kurallar
        if optimize_consistency:
            mov_en = "CRITICAL: Keep the character's body and head movements MINIMAL and SUBTLE (e.g., micro-expressions, gentle blinking, very small head tilts). Do NOT describe large, sudden, or complex movements (like walking, jumping, turning around, waving arms). Large movements cause the AI video engine to morph the face and lose character consistency."
            mov_tr = "KRİTİK: Karakterin vücut ve baş hareketlerini MİNİMAL ve HAFİF tut (örn. mikro mimikler, hafif göz kırpma, çok küçük baş eğme). Büyük, ani veya karmaşık hareketler (yürüme, zıplama, arkasını dönme, el sallama, elleri çok fazla oynatma) KESİNLİKLE TARİF ETME. Büyük hareketler AI'ın yüzü bozmasına ve karakter tutarlılığını kaybetmesine neden olur."
            sp_en = "    - Each clip is only about 5-8 seconds long. If the spoken line is too long it gets cut off mid-sentence and the next scene starts abruptly. This is NOT allowed.\n    - Write a spoken line that can be said CALMLY and COMPLETELY in about 4 to 5 seconds, leaving a silent beat before the clip ends. Never fill the whole clip with talking.\n    - As a concrete guide, this is roughly 6 to 12 words in the SPEECH LANGUAGE, about 1 short sentence. NEVER write more than 14 words for a single scene."
            sp_tr = "    - Her klip yalnızca yaklaşık 5-8 saniye ve Veo sonunda sert keser. Konuşma cümlesi çok uzun olursa cümle ortasından kesilir ve sonraki sahne aniden başlar. BU İSTENMİYOR.\n    - Sakin ve EKSİKSİZ biçimde yaklaşık 4-5 saniyede söylenebilecek bir cümle yaz; klibin sonunda kısa bir sessizlik kalsın. Klibin tamamını konuşmayla DOLDURMA.\n    - Somut ölçü: konuşma dilinde yaklaşık 6-12 kelime, 1 kısa cümle. Tek sahne için ASLA 14 kelimeyi geçme."
            hook_en = "greeting + name + hook together have to be sayable calmly and completely within about 4 to 5 seconds (roughly 6 to 12 words, never more than 14)."
            hook_tr = "selamlama + isim + hook birlikte sakin biçimde yaklaşık 4-5 saniyede eksiksiz söylenebilmeli (yaklaşık 6-12 kelime, asla 14'ü geçme), klip kesilmeden bitmeli."
        else:
            mov_en = "(natural micro head movements, eye contact with the front camera, small shifts in posture, discreet hand gestures that stay mostly out of frame)."
            mov_tr = "(hafif jestler, baş hareketleri, etrafa bakması, küçük bir nesneyi kadrajın kenarında göstermesi vb.) gerçekçi ve kısa olmalı."
            sp_en = "    - Each clip is only about 8 seconds long, and Veo cuts hard at the end. If the spoken line is too long it gets cut off mid-sentence and the next scene starts abruptly. This is NOT allowed.\n    - Write a spoken line that can be said CALMLY and COMPLETELY in about 6 to 7 seconds, leaving a short silent beat (about 1 to 1.5 seconds) before the clip ends. Never fill the whole 8 seconds with talking.\n    - As a concrete guide, this is roughly 12 to 16 words in the SPEECH LANGUAGE, about 1 to 2 short sentences. NEVER write more than 18 words for a single scene."
            sp_tr = "    - Her klip yalnızca yaklaşık 8 saniye ve Veo sonunda sert keser. Konuşma cümlesi çok uzun olursa cümle ortasından kesilir ve sonraki sahne aniden başlar. BU İSTENMİYOR.\n    - Sakin ve EKSİKSİZ biçimde yaklaşık 6-7 saniyede söylenebilecek bir cümle yaz; klibin sonunda yaklaşık 1-1.5 saniyelik kısa bir sessizlik kalsın. 8 saniyenin tamamını konuşmayla DOLDURMA.\n    - Somut ölçü: konuşma dilinde yaklaşık 12-16 kelime, 1-2 kısa cümle. Tek sahne için ASLA 18 kelimeyi geçme."
            hook_en = "greeting + name + hook together have to be sayable calmly and completely within about 6 to 7 seconds (roughly 12 to 16 words, never more than 18)."
            hook_tr = "selamlama + isim + hook birlikte sakin biçimde yaklaşık 6-7 saniyede eksiksiz söylenebilmeli (yaklaşık 12-16 kelime, asla 18'i geçme), klip kesilmeden bitmeli."

        final_system = (
            self.system_prompt
            .replace("__MOVEMENT_RULE__", mov_en)
            .replace("__SPEECH_BUDGET_RULE__", sp_en)
            .replace("__HOOK_BUDGET_RULE__", hook_en)
        )
        user_prompt = (
            user_prompt
            .replace("__MOVEMENT_RULE_TR__", mov_tr)
            .replace("__SPEECH_BUDGET_RULE_TR__", sp_tr)
            .replace("__HOOK_BUDGET_RULE_TR__", hook_tr)
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

        # Kendi metin (script) modu: her sahnenin konuşması kullanıcının yazdığı cümledir.
        # Modelin bu cümleleri AYNEN kullanması, sadece görsel/ortam üretmesi gerekir.
        if scene_lines:
            lines = "\n".join(
                f"- Sahne {i}: {line.strip()}" for i, line in enumerate(scene_lines, 1)
            )
            user_prompt += (
                "\n\nÖNEMLİ — BİREBİR KONUŞMA METNİ (kullanıcının kendi metni):\n"
                "Bu videonun konuşması kullanıcı tarafından yazılmıştır. Her sahnede karakterin "
                "söyleyeceği cümle AŞAĞIDA SIRAYLA verilmiştir. Bu cümleleri KELİMESİ KELİMESİNE, "
                "AYNEN kullan; sözcükleri DEĞİŞTİRME, KISALTMA, EKLEME veya yeniden yazma. "
                "Her image_to_video_prompt içinde ilgili sahnenin konuşmasını SES VE KONUŞMA "
                "kurallarındaki gibi 'word for word: <cümle>' kalıbıyla, verilen cümleyle birebir "
                "aynı yaz. Görsel, ortam, kadraj ve aksiyonu sen üret; ama söylenen sözler bunlar olsun:\n"
                f"{lines}"
            )

        messages = [
            {"role": "system", "content": final_system},
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

    def recommend_scene_count(
        self, video_topic: str, speech_language: str, min_scenes: int, max_scenes: int
    ) -> int:
        """
        Konunun zenginliğine göre ideal sahne sayısını LLM ile önerir (min..max arası).
        Hata olursa makul bir orta değere düşer.
        """
        default = max(min_scenes, min(max_scenes, (min_scenes + max_scenes) // 2))
        prompt = (
            "Bir kısa sosyal medya videosu (Reels/TikTok/Shorts) için sahne sayısı belirleyeceğiz. "
            f"Her sahne ~8 saniyelik tek bir konuşma anıdır. Aşağıdaki konuyu akıcı, doğal bir "
            f"videoya dönüştürmek için ideal sahne sayısını {min_scenes} ile {max_scenes} arasında seç. "
            "Basit/tek mesajlı konular için az, çok adımlı/zengin konular için fazla sahne uygundur. "
            "SADECE bir tam sayı yaz, başka hiçbir şey yazma.\n\n"
            f"Konu:\n{video_topic}\n\n"
            f"Video dili: {speech_language}"
        )
        try:
            raw = self.openai.chat(
                [{"role": "user", "content": prompt}], max_tokens=10
            ).strip()
            m = re.search(r"\d+", raw)
            if not m:
                return default
            n = int(m.group())
            n = max(min_scenes, min(max_scenes, n))
            log.info(f"Önerilen sahne sayısı: {n} (konu: {video_topic[:50]})")
            return n
        except Exception:
            log.warning("Sahne sayısı önerilemedi, varsayılan kullanılacak", exc_info=True)
            return default

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
            vot = scene.get("voiceover_text")
            if not isinstance(t2i, str) or not t2i.strip():
                raise ScenarioError(f"Sahne {i}: text_to_image_prompt boş")
            if not isinstance(i2v, str) or not i2v.strip():
                raise ScenarioError(f"Sahne {i}: image_to_video_prompt boş")
            if not isinstance(vot, str) or not vot.strip():
                raise ScenarioError(f"Sahne {i}: voiceover_text boş")
            # Kelime sayısı kontrolü — çok uzun cümleler Veo'da kesilir
            word_count = len(vot.split())
            if word_count > 18:
                raise ScenarioError(
                    f"Sahne {i}: voiceover_text çok uzun ({word_count} kelime, max 18). "
                    f"Cümle 8 saniyeye sığmayacak ve ortasından kesilecek. Daha kısa yaz."
                )

        caption = result.get("video_caption")
        if not isinstance(caption, str) or not caption.strip():
            raise ScenarioError("video_caption boş")
