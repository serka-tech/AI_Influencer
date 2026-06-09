"""
Replicate Service — Video + Ses Birleştirme
=============================================
Replicate API ile video ve ses dosyalarını birleştirir.
Model: lucataco/video-audio-merge
Cloud-based — Railway'de FFmpeg kurulumu gereksiz.
"""

from __future__ import annotations

import io
import time

import replicate

from logger import get_logger

log = get_logger("replicate_service")

# Polling ayarları
POLL_INTERVAL_SECONDS = 5
# 60 deneme × 5s = 5 dakika. Real Replicate merge süreleri tipik 30-90s,
# 5 dakika 3-4x headroom verir. Railway worker graceful shutdown ~60s
# olduğundan 10dk merge ortasında deploy bug'ı oluşturuyordu.
MAX_POLL_ATTEMPTS = 60
# Async wrapper hard ceiling (saniye). Polling bekleme + reload retry'ları
# beklenmedik şekilde uzarsa asyncio.timeout() kesin keser → session lock serbest.
ASYNC_MERGE_HARD_TIMEOUT = 300  # 5 dakika


class ReplicateService:
    """Replicate API ile video + ses birleştirme."""

    def __init__(self, api_token: str):
        self.client = replicate.Client(api_token=api_token)

    def upload_audio(self, audio_bytes: bytes, filename: str = "audio.mp3") -> str:
        """
        Ses dosyasını Replicate file storage'a yükler ve URL döner.
        Data URI yerine gerçek URL kullanmak için zorunlu.

        Returns:
            str: Replicate'in erişebileceği ses dosyası URL'i
        """
        file_obj = io.BytesIO(audio_bytes)
        uploaded = self.client.files.create(file_obj, filename=filename)
        url = uploaded.urls.get("get") if hasattr(uploaded, "urls") else str(uploaded)
        log.info(f"Ses Replicate storage'a yüklendi: {str(url)[:80]}")
        return str(url)

    async def async_upload_audio(self, audio_bytes: bytes, filename: str = "audio.mp3") -> str:
        """Async wrapper — event loop'u bloke etmez."""
        import asyncio as _asyncio
        return await _asyncio.to_thread(self.upload_audio, audio_bytes, filename)

    # Voice (dış ses) için varsayılan amplifikasyon.
    # Replicate `lucataco/video-audio-merge` modeli ffmpeg amix kullanır:
    # mix(voice * audio_volume, ambient * 1.0) ve sonra ortalama alır.
    # 2.5 değeri ile karakter sesi ambient (raket/top vb. efektler) üzerinde
    # net duyulur. Aşırı (>3.0) clipping/distortion riski artar.
    # Aralık: model şeması 0–5; 2.0–3.0 arası "voice-forward" sweet spot.
    DEFAULT_VOICE_VOLUME = 2.5
    VIDEO_AUDIO_MERGE_VERSION = "8c3d57c9c9a1aaa05feabafbcd2dff9f68a5cb394e54ec020c1c2dcc42bde109"

    def merge_video_audio(
        self,
        video_url: str,
        audio_url: str,
        replace_audio: bool = False,
        duration_mode: str = "audio",
        audio_volume: float | None = None,
    ) -> str:
        """
        Video ve ses dosyalarını birleştirir.

        Args:
            video_url: Video dosyası URL'i (mp4)
            audio_url: Ses dosyası URL'i (mp3, wav)
            replace_audio: True = videonun orijinal sesini kaldır,
                          False = dış sesi video ambient sesinin üzerine ekle
            duration_mode: "audio" | "video" — uzunluk farkı yönetimi
            audio_volume: Voice (dış ses) çarpanı. None → DEFAULT_VOICE_VOLUME.
                         1.0 = orijinal, 2.5 = ambient'e göre baskın voice.

        Returns:
            str: Birleştirilmiş video URL'i

        Raises:
            Exception: Birleştirme başarısız olursa
        """
        if audio_volume is None:
            audio_volume = self.DEFAULT_VOICE_VOLUME
        # Replicate model şeması: 0–5. Aralık dışı clamp et.
        audio_volume = max(0.0, min(5.0, float(audio_volume)))

        prediction = None
        completed = False
        try:
            log.info(
                f"Video+ses birleştirme başlatılıyor: "
                f"replace_audio={replace_audio}, audio_volume={audio_volume}"
            )

            prediction = self.client.predictions.create(
                version=self.VIDEO_AUDIO_MERGE_VERSION,
                input={
                    "video_file": video_url,
                    "audio_file": audio_url,
                    "replace_audio": replace_audio,
                    "duration_mode": duration_mode,
                    "audio_volume": audio_volume,
                },
            )

            log.info(f"Replicate prediction oluşturuldu: {prediction.id} (duration_mode={duration_mode})")

            # Polling
            for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
                prediction.reload()

                if prediction.status == "succeeded":
                    output_url = prediction.output
                    if output_url is None:
                        raise RuntimeError(f"Replicate succeeded ama output None: {prediction.id}")
                    if isinstance(output_url, list):
                        output_url = output_url[0] if output_url else None
                    if output_url is None:
                        raise RuntimeError(f"Replicate succeeded ama output boş liste: {prediction.id}")
                    # Replicate SDK v1.x FileOutput objesi dönebilir — str cast
                    output_url = str(output_url)
                    # URL validasyonu
                    if not output_url.startswith("http"):
                        raise RuntimeError(f"Replicate geçersiz output URL: {output_url[:100]}")
                    log.info(
                        f"Video+ses birleştirme tamamlandı: {prediction.id} "
                        f"({attempt} deneme)"
                    )
                    completed = True
                    return output_url

                if prediction.status == "failed":
                    completed = True
                    error = prediction.error or "Bilinmeyen hata"
                    log.error(f"Replicate başarısız: {prediction.id} — {error}")
                    raise RuntimeError(f"Replicate merge başarısız: {error}")

                if prediction.status == "canceled":
                    completed = True
                    raise RuntimeError("Replicate görev iptal edildi")

                log.info(
                    f"Replicate polling [{attempt}/{MAX_POLL_ATTEMPTS}]: "
                    f"status={prediction.status}"
                )
                time.sleep(POLL_INTERVAL_SECONDS)

            raise TimeoutError(
                f"Replicate timeout: {prediction.id} — "
                f"{MAX_POLL_ATTEMPTS} deneme aşıldı"
            )

        except (RuntimeError, TimeoutError):
            raise
        except Exception:
            log.error("Replicate birleştirme genel hatası", exc_info=True)
            raise
        finally:
            if prediction is not None and not completed:
                try:
                    prediction.cancel()
                    log.warning(f"Replicate prediction iptal edildi (cleanup): {prediction.id}")
                except Exception:
                    log.warning(f"Replicate prediction cancel başarısız: {getattr(prediction, 'id', '?')}", exc_info=True)

    async def async_merge_video_audio(
        self,
        video_url: str,
        audio_url: str,
        replace_audio: bool = False,
        duration_mode: str = "audio",
        audio_volume: float | None = None,
    ) -> str:
        """
        Video ve ses dosyalarını async olarak birleştirir.
        time.sleep() yerine asyncio.sleep() — event loop bloklanmaz.

        Hard timeout: ASYNC_MERGE_HARD_TIMEOUT (5 dakika) — Railway worker
        graceful shutdown'ı (~60s) sırasında 10dk polling deadlock'unu önler.
        Timeout durumunda finally bloğu Replicate prediction'ını iptal eder
        ve TimeoutError yukarı propagate edilir → caller temiz mesaj gösterir.

        Args:
            audio_volume: Voice (dış ses) çarpanı. None → DEFAULT_VOICE_VOLUME (2.5).

        Returns:
            str: Birleştirilmiş video URL'i

        Raises:
            TimeoutError: ASYNC_MERGE_HARD_TIMEOUT aşılırsa
        """
        import asyncio as _asyncio

        if audio_volume is None:
            audio_volume = self.DEFAULT_VOICE_VOLUME
        audio_volume = max(0.0, min(5.0, float(audio_volume)))

        prediction = None
        completed = False
        try:
            async with _asyncio.timeout(ASYNC_MERGE_HARD_TIMEOUT):
                log.info(
                    f"Async video+ses birleştirme başlatılıyor: "
                    f"replace_audio={replace_audio}, audio_volume={audio_volume}, "
                    f"hard_timeout={ASYNC_MERGE_HARD_TIMEOUT}s"
                )

                prediction = await _asyncio.to_thread(
                    self.client.predictions.create,
                    version=self.VIDEO_AUDIO_MERGE_VERSION,
                    input={
                        "video_file": video_url,
                        "audio_file": audio_url,
                        "replace_audio": replace_audio,
                        "duration_mode": duration_mode,
                        "audio_volume": audio_volume,
                    },
                )

                log.info(f"Replicate prediction oluşturuldu: {prediction.id} (duration_mode={duration_mode})")

                # Async polling — reload geçici hata toleransı + adaptif interval
                reload_failures = 0
                MAX_RELOAD_FAILURES = 3
                prev_status: str | None = None

                for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
                    # Geçici reload hatalarını birkaç kez tolere et (1-2 hata sorun değil)
                    try:
                        await _asyncio.to_thread(prediction.reload)
                        reload_failures = 0
                    except Exception as reload_err:
                        reload_failures += 1
                        log.warning(
                            f"Replicate reload geçici hata "
                            f"({reload_failures}/{MAX_RELOAD_FAILURES}): {reload_err}"
                        )
                        if reload_failures >= MAX_RELOAD_FAILURES:
                            log.error(
                                f"Replicate reload {MAX_RELOAD_FAILURES} kez ardışık başarısız"
                            )
                            raise RuntimeError(
                                f"Replicate reload tekrar tekrar başarısız: {reload_err}"
                            )
                        await _asyncio.sleep(2)
                        continue

                    if prediction.status == "succeeded":
                        output_url = prediction.output
                        if output_url is None:
                            raise RuntimeError(f"Replicate succeeded ama output None: {prediction.id}")
                        if isinstance(output_url, list):
                            output_url = output_url[0] if output_url else None
                        if output_url is None:
                            raise RuntimeError(f"Replicate succeeded ama output boş liste: {prediction.id}")
                        output_url = str(output_url)
                        if not output_url.startswith("http"):
                            raise RuntimeError(f"Replicate geçersiz output URL: {output_url[:100]}")
                        log.info(
                            f"Video+ses birleştirme tamamlandı: {prediction.id} "
                            f"({attempt} deneme)"
                        )
                        completed = True
                        return output_url

                    if prediction.status == "failed":
                        completed = True
                        error = prediction.error or "Bilinmeyen hata"
                        log.error(f"Replicate başarısız: {prediction.id} — {error}")
                        raise RuntimeError(f"Replicate merge başarısız: {error}")

                    if prediction.status == "canceled":
                        completed = True
                        raise RuntimeError("Replicate görev iptal edildi")

                    if prediction.status != prev_status:
                        log.info(
                            f"Replicate polling [{attempt}/{MAX_POLL_ATTEMPTS}]: "
                            f"status {prev_status}→{prediction.status}"
                        )
                        prev_status = prediction.status
                    else:
                        log.debug(
                            f"Replicate polling [{attempt}/{MAX_POLL_ATTEMPTS}]: "
                            f"status={prediction.status}"
                        )
                    # Adaptif: ilk reload hızlı (2s), sonrası POLL_INTERVAL_SECONDS (5s)
                    interval = 2 if attempt == 1 else POLL_INTERVAL_SECONDS
                    await _asyncio.sleep(interval)

                raise TimeoutError(
                    f"Replicate timeout: {prediction.id} — "
                    f"{MAX_POLL_ATTEMPTS} deneme aşıldı"
                )

        except (RuntimeError, TimeoutError) as e:
            # NOTE: Python 3.11+'da asyncio.TimeoutError == TimeoutError.
            # Hem asyncio.timeout() iptali hem de inner polling TimeoutError'u
            # buradan geçer; mesajlar zaten net olduğu için tek branch yeterli.
            if isinstance(e, TimeoutError):
                pid = getattr(prediction, "id", "?") if prediction else "?"
                log.error(
                    f"Replicate merge timeout: hard_cap={ASYNC_MERGE_HARD_TIMEOUT}s, "
                    f"prediction={pid}"
                )
            raise
        except Exception:
            log.error("Replicate async birleştirme genel hatası", exc_info=True)
            raise
        finally:
            # WHY: Pipeline cancel/timeout olursa Replicate üzerindeki açık prediction'ı iptal et
            if prediction is not None and not completed:
                try:
                    await _asyncio.to_thread(prediction.cancel)
                    log.warning(f"Replicate prediction iptal edildi (cleanup): {prediction.id}")
                except Exception:
                    log.warning(f"Replicate prediction cancel başarısız: {getattr(prediction, 'id', '?')}", exc_info=True)

    def get_prediction_status(self, prediction_id: str) -> dict:
        """
        Mevcut prediction durumunu sorgular.

        Args:
            prediction_id: Prediction ID

        Returns:
            dict: {"status": "...", "output": "...", "error": "..."}
        """
        try:
            prediction = self.client.predictions.get(prediction_id)
            return {
                "status": prediction.status,
                "output": prediction.output,
                "error": prediction.error,
            }
        except Exception:
            log.error(f"Prediction sorgulama hatası: {prediction_id}", exc_info=True)
            return {"status": "error", "output": None, "error": "Sorgulama başarısız"}

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 🔗 VIDEO CONCAT — Multi-Scene Birleştirme
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Model: lucataco/video-merge
    # UGC pipeline oturumundan (23 Nisan 2026) öğrenilen model ve format.
    # Input: video URL'leri array olarak (["url1", "url2", ...]) — pipe string değil

    VIDEO_MERGE_VERSION = "14273448a57117b5d424410e2e79700ecde6cc7d60bf522a769b9c7cf989eba7"

    def concat_videos(self, video_urls: list[str]) -> str:
        """
        Birden fazla video dosyasını sırayla birleştirir (concat).

        Args:
            video_urls: Video URL'leri listesi (en az 2, max 10)

        Returns:
            str: Birleştirilmiş video URL'i

        Raises:
            ValueError: 2'den az video verilirse
            RuntimeError: Birleştirme başarısız olursa
        """
        if len(video_urls) < 2:
            raise ValueError(f"Concat için en az 2 video gerekli, {len(video_urls)} verildi")

        log.info(f"Video concat başlatılıyor: {len(video_urls)} video")

        prediction = None
        completed = False
        try:
            prediction = self.client.predictions.create(
                version=self.VIDEO_MERGE_VERSION,
                input={"video_files": video_urls},
            )
            log.info(f"Concat prediction oluşturuldu: {prediction.id}")

            for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
                prediction.reload()

                if prediction.status == "succeeded":
                    output_url = prediction.output
                    if isinstance(output_url, list):
                        output_url = output_url[0] if output_url else None
                    output_url = str(output_url) if output_url else None
                    if not output_url or not output_url.startswith("http"):
                        raise RuntimeError(f"Concat geçersiz output: {output_url}")
                    log.info(f"Video concat tamamlandı: {prediction.id} ({attempt} deneme)")
                    completed = True
                    return output_url

                if prediction.status in ("failed", "canceled"):
                    completed = True
                    error = prediction.error or "Bilinmeyen hata"
                    raise RuntimeError(f"Video concat başarısız: {error}")

                log.info(f"Concat polling [{attempt}/{MAX_POLL_ATTEMPTS}]: status={prediction.status}")
                time.sleep(POLL_INTERVAL_SECONDS)

            raise TimeoutError(f"Concat timeout: {prediction.id}")

        except (RuntimeError, TimeoutError, ValueError):
            raise
        except Exception:
            log.error("Video concat genel hatası", exc_info=True)
            raise
        finally:
            if prediction is not None and not completed:
                try:
                    prediction.cancel()
                    log.warning(f"Concat prediction iptal edildi (cleanup): {prediction.id}")
                except Exception:
                    log.warning(f"Concat prediction cancel başarısız: {getattr(prediction, 'id', '?')}", exc_info=True)

    async def async_concat_videos(self, video_urls: list[str]) -> str:
        """
        Birden fazla video dosyasını async olarak birleştirir.
        asyncio.sleep() kullanır → event loop'u bloke etmez.

        Args:
            video_urls: Video URL'leri listesi (en az 2)

        Returns:
            str: Birleştirilmiş video URL'i
        """
        import asyncio as _asyncio

        if len(video_urls) < 2:
            raise ValueError(f"Concat için en az 2 video gerekli, {len(video_urls)} verildi")

        log.info(f"Async video concat başlatılıyor: {len(video_urls)} video")

        prediction = None
        completed = False
        try:
            prediction = await _asyncio.to_thread(
                self.client.predictions.create,
                version=self.VIDEO_MERGE_VERSION,
                input={"video_files": video_urls},
            )
            log.info(f"Concat prediction oluşturuldu: {prediction.id}")

            for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
                await _asyncio.to_thread(prediction.reload)

                if prediction.status == "succeeded":
                    output_url = prediction.output
                    if isinstance(output_url, list):
                        output_url = output_url[0] if output_url else None
                    output_url = str(output_url) if output_url else None
                    if not output_url or not output_url.startswith("http"):
                        raise RuntimeError(f"Concat geçersiz output: {output_url}")
                    log.info(f"Async concat tamamlandı: {prediction.id} ({attempt} deneme)")
                    completed = True
                    return output_url

                if prediction.status in ("failed", "canceled"):
                    completed = True
                    error = prediction.error or "Bilinmeyen hata"
                    raise RuntimeError(f"Video concat başarısız: {error}")

                log.info(f"Concat polling [{attempt}/{MAX_POLL_ATTEMPTS}]: status={prediction.status}")
                await _asyncio.sleep(POLL_INTERVAL_SECONDS)

            raise TimeoutError(f"Concat timeout: {prediction.id}")

        except (RuntimeError, TimeoutError, ValueError):
            raise
        except Exception:
            log.error("Async video concat genel hatası", exc_info=True)
            raise
        finally:
            # WHY: Pipeline cancel/timeout olursa Replicate üzerindeki açık prediction'ı iptal et
            if prediction is not None and not completed:
                try:
                    await _asyncio.to_thread(prediction.cancel)
                    log.warning(f"Concat prediction iptal edildi (cleanup): {prediction.id}")
                except Exception:
                    log.warning(f"Concat prediction cancel başarısız: {getattr(prediction, 'id', '?')}", exc_info=True)
