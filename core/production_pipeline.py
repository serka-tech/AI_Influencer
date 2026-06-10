from __future__ import annotations

"""
Production Pipeline — Görsel → Video → Birleştirme Orkestratörü
=================================================================
n8n akışındaki şu zincirin kod karşılığı:
  Nano Banana Pro (görsel) → Görseli Al → Veo 3.1 (video) → Videoyu Al
  → URL'leri Birleştir → Klipleri Birleştir (Replicate concat)

Her sahne için:
  1. Nano Banana Pro ile ilk kare görseli (karakter referansı + text_to_image_prompt)
  2. Veo 3.1 ile o görselden 8 sn'lik video (image_to_video_prompt, native ses)
Sahneler paralel üretilir (asyncio.gather); sonra sırayla concat edilir.

Tek sahnede concat gerekmez → o sahnenin videosu doğrudan döner.
"""

import asyncio

from logger import get_logger
from services.kie_api import KieAIService
from services.replicate_service import ReplicateService

log = get_logger("production_pipeline")


class PipelineError(Exception):
    """Pipeline aşamalarından biri başarısız olduğunda fırlatılır."""

    def __init__(self, message: str, stage: str, code: str | None = None):
        super().__init__(message)
        self.stage = stage          # "image" | "video" | "merge"
        self.code = code            # Kie failCode vb. (opsiyonel)


class ProductionPipeline:
    def __init__(
        self,
        kie: KieAIService,
        replicate: ReplicateService,
        elevenlabs: ElevenLabsService,
        reference_image_url: str,
        aspect_ratio: str = "9:16",
        resolution: str = "1K",
    ):
        self.kie = kie
        self.replicate = replicate
        self.elevenlabs = elevenlabs
        self.reference_image_url = reference_image_url
        self.aspect_ratio = aspect_ratio
        self.resolution = resolution

    async def _produce_scene(self, index: int, scene: dict, progress=None) -> str:
        """Tek bir sahne için görsel → ses → video → lipsync üretir."""
        import asyncio
        t2i = scene["text_to_image_prompt"]
        i2v = scene["image_to_video_prompt"]
        voice_text = scene.get("voiceover_text") or scene.get("caption") or scene.get("voiceover", i2v)

        image_input = [self.reference_image_url]
        place_ref = scene.get("reference_image_url")
        if place_ref:
            image_input.append(place_ref)
            log.info(f"Sahne {index}: mekan referansı eklendi → {str(place_ref)[:70]}")

        # 1) Görsel Üretimi (Nano Banana Pro)
        log.info(f"Sahne {index}: görsel üretimi başlıyor")
        image_task_id = await asyncio.to_thread(
            self.kie.create_image,
            prompt=t2i,
            image_input=image_input,
            aspect_ratio=self.aspect_ratio,
            resolution=self.resolution,
            output_format="png",
        )
        image_result = await self.kie.async_poll_task(image_task_id)
        if image_result.get("status") != "success" or not image_result.get("urls"):
            raise PipelineError(f"Sahne {index} görsel üretimi başarısız: {image_result.get('error', '?')}", stage="image")
        image_url = image_result["urls"][0]
        if progress: await progress(f"🖼️ Sahne {index} görseli hazır")

        # 2) Ses Üretimi (ElevenLabs)
        log.info(f"Sahne {index}: ses üretimi başlıyor")
        try:
            audio_bytes = await self.elevenlabs.generate_audio(voice_text)
            audio_url = await self.replicate.async_upload_audio(audio_bytes, f"scene_{index}_voice.mp3")
            if progress: await progress(f"🎙️ Sahne {index} sesi ElevenLabs ile üretildi")
        except Exception as e:
            raise PipelineError(f"Sahne {index} ses üretimi başarısız: {e}", stage="audio")

        # 3) Baz Video Üretimi (Kling 3.0 via Kie AI)
        for video_attempt in range(1, 4):
            log.info(f"Sahne {index}: Kling 3.0 baz video üretimi başlıyor (Deneme {video_attempt})")
            try:
                # İlk denemede orijinal prompt, sonrakilerde hafif varyasyon
                current_prompt = i2v if video_attempt == 1 else f"{i2v} [v{video_attempt}]"
                
                video_task_id = await asyncio.to_thread(
                    self.kie.create_kling_task,
                    prompt=current_prompt,
                    image_url=image_url
                )
                video_result = await self.kie.async_poll_task(video_task_id)
                if video_result.get("status") == "success" and video_result.get("urls"):
                    base_video_url = video_result["urls"][0]
                    if progress: await progress(f"🎬 Sahne {index} Kling baz videosu hazır")
                    break # Başarılı oldu, döngüden çık
                else:
                    err_msg = video_result.get('error', '?')
                    log.warning(f"Sahne {index} Kling video üretimi başarısız (Deneme {video_attempt}): {err_msg}")
                    if video_attempt == 3:
                        raise PipelineError(f"Sahne {index} Kling video üretimi 3 denemede de başarısız: {err_msg}", stage="video")
            except PipelineError:
                raise
            except Exception as e:
                log.warning(f"Sahne {index} Kling video üretimi sırasında exception (Deneme {video_attempt}): {e}")
                if video_attempt == 3:
                    raise PipelineError(f"Sahne {index} Kling video üretimi başarısız: {e}", stage="video")
            
            # Yeniden denemeden önce bekle ve kullanıcıya bildir
            if progress:
                await progress(f"⚠️ Sahne {index} Kling video üretimi takıldı, tekrar deneniyor ({video_attempt}/3)...")
            await asyncio.sleep(5)

        # 4) Lip-Sync (Replicate cjwbw/video-retalking)
        log.info(f"Sahne {index}: Lip-Sync birleştirme başlıyor")
        try:
            if progress: await progress(f"👄 Sahne {index} Dudak senkronizasyonu yapılıyor (Lip-Sync)...")
            final_video_url = await self.replicate.async_lip_sync(base_video_url, audio_url)
            if progress: await progress(f"✅ Sahne {index} tamamlandı!")
            return final_video_url
        except Exception as e:
            log.warning(f"Lip-Sync başarısız oldu, baz video geri dönülüyor: {e}")
            # Lip-Sync patlarsa en azından elimizde video var, merge_video_audio ile sesi üstüne yapıştıralım
            fallback_url = await self.replicate.async_merge_video_audio(base_video_url, audio_url, replace_audio=True)
            return fallback_url

    async def run(self, scenario: dict, progress=None) -> str:
        """
        Tüm senaryoyu üretir ve birleştirilmiş tek video URL'i döner.

        Args:
            scenario: {"scenes": [...], "video_caption": "..."}
            progress: opsiyonel async callback(msg: str) — ilerleme bildirimi

        Returns:
            str: Birleştirilmiş (veya tek sahne ise tek) video URL'i
        """
        scenes = scenario["scenes"]
        log.info(f"Pipeline başlıyor: {len(scenes)} sahne")

        # Sahneleri paralel üret — sıra korunur (gather sırayı garanti eder)
        tasks = [self._produce_scene(i, s, progress) for i, s in enumerate(scenes, 1)]
        video_urls = await asyncio.gather(*tasks)
        video_urls = list(video_urls)

        if len(video_urls) == 1:
            log.info("Tek sahne — concat atlanıyor")
            return video_urls[0]

        # Birleştirme — Replicate concat
        if progress:
            await progress("🔗 Sahneler birleştiriliyor")
        log.info(f"Concat başlıyor: {len(video_urls)} video")
        try:
            final_url = await self.replicate.async_concat_videos(video_urls)
        except Exception as e:
            raise PipelineError(f"Video birleştirme başarısız: {e}", stage="merge") from e

        log.info(f"Pipeline tamamlandı → {final_url[:70]}")
        return final_url
