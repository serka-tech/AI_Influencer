from __future__ import annotations

"""
Production Pipeline — Görsel → Video → Birleştirme Orkestratörü
=================================================================
Her sahne için:
  1. Nano Banana Pro ile ilk kare görseli (karakter referansı + text_to_image_prompt)
  2. Veo 3.1 ile o görselden 8 sn'lik video (image_to_video_prompt, native ses)

Sahneler SIRALI üretilir (identity chaining): her sahnenin üretilen görseli,
bir sonraki sahneye ek referans olarak geçer → karakter tutarlılığı korunur.
Bir sahne başarısız olursa atlanır, diğerleriyle devam edilir.

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
        reference_image_url: str,
        elevenlabs: ElevenLabsService | None = None,
        engine: str = "veo",
        aspect_ratio: str = "9:16",
        resolution: str = "1K",
    ):
        self.kie = kie
        self.replicate = replicate
        self.elevenlabs = elevenlabs
        self.engine = (engine or "veo").lower()
        self.reference_image_url = reference_image_url
        self.aspect_ratio = aspect_ratio
        self.resolution = resolution
        if self.engine == "kling" and self.elevenlabs is None:
            raise ValueError("engine='kling' için ElevenLabsService gerekli")

    async def _produce_scene(self, index: int, scene: dict, progress=None, image_cache: dict | None = None, prev_scene_image_url: str | None = None) -> tuple[str, str]:
        """Tek sahne: önce görsel üretir, sonra seçili motora göre video üretir."""
        t2i = scene["text_to_image_prompt"]

        image_input = [self.reference_image_url]
        # Identity chaining: önceki sahnenin görselini referans olarak ekle
        # Bu, Nano Banana Pro'nun karakter kimliğini sahneler arası korumasını sağlar
        if prev_scene_image_url:
            image_input.append(prev_scene_image_url)
            log.info(f"Sahne {index}: önceki sahne referansı eklendi (identity chaining)")
        place_ref = scene.get("reference_image_url")
        if place_ref:
            image_input.append(place_ref)
            log.info(f"Sahne {index}: mekan referansı eklendi → {str(place_ref)[:70]}")

        # Görsel Cache Kontrolü
        cache_key = f"{t2i}|{self.aspect_ratio}|{self.resolution}|{place_ref or ''}|{prev_scene_image_url or ''}"
        if image_cache is not None and cache_key in image_cache:
            image_url = image_cache[cache_key]
            log.info(f"Sahne {index}: görsel önbellekten alındı")
            if progress: await progress(f"🖼️ Sahne {index} görseli hazır (önbellekten)")
        else:
            # 1) Görsel Üretimi (Nano Banana Pro) — her iki motor için ortak ilk kare
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
            if image_cache is not None:
                image_cache[cache_key] = image_url
            if progress: await progress(f"🖼️ Sahne {index} görseli hazır")

        if self.engine == "kling":
            video_url = await self._produce_scene_kling(index, scene, image_url, progress)
        else:
            video_url = await self._produce_scene_veo(index, scene, image_url, progress)
        return video_url, image_url

    async def _produce_scene_veo(self, index: int, scene: dict, image_url: str, progress=None) -> str:
        """Veo 3.1: ilk kareden kendi sesiyle video (ElevenLabs/lip-sync yok)."""
        i2v = scene["image_to_video_prompt"]
        for attempt in range(1, 4):
            log.info(f"Sahne {index}: Veo 3.1 video üretimi (Deneme {attempt})")
            try:
                video_task_id = await asyncio.to_thread(
                    self.kie.create_veo_video,
                    prompt=i2v,
                    image_url=image_url,
                    aspect_ratio=self.aspect_ratio,
                )
                video_result = await self.kie.async_poll_veo_task(video_task_id)
                if video_result.get("status") == "success" and video_result.get("urls"):
                    if progress: await progress(f"🎬 Sahne {index} videosu hazır")
                    return video_result["urls"][0]
                err_msg = video_result.get("error", "?")
                code = str(video_result.get("code", ""))
                log.warning(f"Sahne {index} Veo başarısız (Deneme {attempt}): {err_msg}")
                if attempt == 3:
                    raise PipelineError(f"Sahne {index} Veo video üretimi başarısız: {err_msg}", stage="video", code=code)
            except PipelineError:
                raise
            except Exception as e:
                log.warning(f"Sahne {index} Veo video exception (Deneme {attempt}): {e}")
                if attempt == 3:
                    raise PipelineError(f"Sahne {index} Veo video üretimi başarısız: {e}", stage="video")
            if progress:
                await progress(f"⚠️ Sahne {index} video takıldı, tekrar deneniyor ({attempt}/3)...")
            await asyncio.sleep(5)
        raise PipelineError(f"Sahne {index} Veo video üretimi başarısız", stage="video")

    async def _produce_scene_kling(self, index: int, scene: dict, image_url: str, progress=None) -> str:
        """Kling 3.0 sessiz video → ElevenLabs Türkçe ses → Replicate lip-sync."""
        i2v = scene["image_to_video_prompt"]
        voice_text = scene.get("voiceover_text") or scene.get("caption") or scene.get("voiceover", i2v)

        # Ses Üretimi (ElevenLabs)
        log.info(f"Sahne {index}: ses üretimi başlıyor")
        try:
            audio_bytes = await self.elevenlabs.generate_audio(voice_text)
            audio_url = await self.replicate.async_upload_audio(audio_bytes, f"scene_{index}_voice.mp3")
            if progress: await progress(f"🎙️ Sahne {index} sesi ElevenLabs ile üretildi")
        except Exception as e:
            raise PipelineError(f"Sahne {index} ses üretimi başarısız: {e}", stage="audio")

        # Baz Video Üretimi (Kling 3.0 via Kie AI)
        base_video_url = None
        for video_attempt in range(1, 4):
            log.info(f"Sahne {index}: Kling 3.0 baz video üretimi başlıyor (Deneme {video_attempt})")
            try:
                current_prompt = i2v if video_attempt == 1 else f"{i2v} [v{video_attempt}]"
                video_task_id = await asyncio.to_thread(
                    self.kie.create_kling_task,
                    prompt=current_prompt,
                    image_url=image_url,
                )
                video_result = await self.kie.async_poll_task(video_task_id)
                if video_result.get("status") == "success" and video_result.get("urls"):
                    base_video_url = video_result["urls"][0]
                    if progress: await progress(f"🎬 Sahne {index} Kling baz videosu hazır")
                    break
                err_msg = video_result.get('error', '?')
                log.warning(f"Sahne {index} Kling video başarısız (Deneme {video_attempt}): {err_msg}")
                if video_attempt == 3:
                    raise PipelineError(f"Sahne {index} Kling video üretimi 3 denemede de başarısız: {err_msg}", stage="video")
            except PipelineError:
                raise
            except Exception as e:
                log.warning(f"Sahne {index} Kling video exception (Deneme {video_attempt}): {e}")
                if video_attempt == 3:
                    raise PipelineError(f"Sahne {index} Kling video üretimi başarısız: {e}", stage="video")
            if progress:
                await progress(f"⚠️ Sahne {index} Kling video takıldı, tekrar deneniyor ({video_attempt}/3)...")
            await asyncio.sleep(5)

        # Lip-Sync (Replicate cjwbw/video-retalking) — patlarsa sesi merge ile bindir
        log.info(f"Sahne {index}: Lip-Sync birleştirme başlıyor")
        try:
            if progress: await progress(f"👄 Sahne {index} dudak senkronizasyonu (Lip-Sync)...")
            final_video_url = await self.replicate.async_lip_sync(base_video_url, audio_url)
            if progress: await progress(f"✅ Sahne {index} tamamlandı!")
            return final_video_url
        except Exception as e:
            log.warning(f"Lip-Sync başarısız, baz videoya ses bindiriliyor: {e}")
            fallback_url = await self.replicate.async_merge_video_audio(base_video_url, audio_url, replace_audio=True)
            return fallback_url

    async def run(
        self, 
        scenario: dict, 
        progress: Callable | None = None, 
        image_cache: dict | None = None,
        use_bgm: bool = False
    ) -> str:
        """
        Tüm senaryoyu üretir ve birleştirilmiş tek video URL'i döner.

        Args:
            scenario: {"scenes": [...], "video_caption": "..."}
            progress: opsiyonel async callback(msg: str) — ilerleme bildirimi
            image_cache: opsiyonel önbellek dict'i (aynı prompt için tekrar üretmemek adına)

        Returns:
            str: Birleştirilmiş (veya tek sahne ise tek) video URL'i
        """
        scenes = scenario["scenes"]
        log.info(f"Pipeline başlıyor: {len(scenes)} sahne (sıralı üretim, identity chaining)")

        # Sahneleri SIRALI üret — her sahnenin görseli sonraki sahneye referans olarak geçer
        # Bu, karakter tutarlılığını büyük ölçüde artırır (identity chaining)
        # Hata toleransı: bir sahne patlarsa atla, diğerleriyle devam et
        video_urls = []
        prev_image_url = None
        failed_scenes = []
        for i, scene in enumerate(scenes, 1):
            try:
                video_url, scene_image_url = await self._produce_scene(
                    i, scene, progress, image_cache, prev_scene_image_url=prev_image_url
                )
                video_urls.append(video_url)
                prev_image_url = scene_image_url  # Chaining: başarılı sahnenin görseli
            except PipelineError as e:
                failed_scenes.append(i)
                log.error(f"Sahne {i} başarısız, atlanıyor: {e}")
                if progress:
                    await progress(
                        f"⚠️ Sahne {i} başarısız ({e.stage}), atlanıyor — "
                        f"diğer sahnelerle devam ediliyor"
                    )
                # prev_image_url korunur — son başarılı sahnenin görseli zincire devam eder

        if not video_urls:
            raise PipelineError(
                f"Tüm sahneler başarısız oldu ({len(scenes)} sahne denenip hepsi hata verdi)",
                stage="video"
            )

        if failed_scenes:
            log.warning(
                f"Pipeline kısmi başarı: {len(video_urls)}/{len(scenes)} sahne başarılı, "
                f"başarısız sahneler: {failed_scenes}"
            )
            if progress:
                await progress(
                    f"ℹ️ {len(video_urls)}/{len(scenes)} sahne başarılı "
                    f"(sahne {', '.join(map(str, failed_scenes))} atlandı)"
                )

        if len(video_urls) == 1:
            log.info("Tek sahne — concat atlanıyor")
            final_url = video_urls[0]
        else:
            # Birleştirme — Replicate concat
            if progress:
                await progress("🔗 Sahneler birleştiriliyor")
            log.info(f"Concat başlıyor: {len(video_urls)} video")
            try:
                final_url = await self.replicate.async_concat_videos(video_urls)
            except Exception as e:
                raise PipelineError(f"Video birleştirme başarısız: {e}", stage="merge") from e

        # Arka plan müziği ekleme (miksleme)
        if use_bgm:
            if progress: await progress("🎵 Arka plan müziği miksleniyor...")
            from config import settings
            try:
                final_url = await self.replicate.async_merge_video_audio(
                    video_url=final_url,
                    audio_url=settings.DEFAULT_BGM_URL,
                    replace_audio=False,
                    duration_mode="video",
                    audio_volume=0.3  # Düşük ses, dış sesin üstüne geçmemesi için
                )
            except Exception as e:
                log.warning("Arka plan müziği miksi başarısız oldu, orijinal video kullanılacak", exc_info=True)
                if progress: await progress("⚠️ Müzik eklenemedi, orijinal video ile devam ediliyor.")

        log.info(f"Pipeline tamamlandı → {final_url[:70]}")
        return final_url
