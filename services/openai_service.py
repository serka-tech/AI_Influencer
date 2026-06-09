from __future__ import annotations

"""
OpenAI Service — GPT-4.1 Mini Chat + Vision
=============================================
Kullanıcıyla doğal sohbet yönetimi ve ürün görseli analizi.
Senaryo üretimi, bilgi çıkarma, prompt oluşturma.
"""

import base64
import json

import openai

from logger import get_logger

log = get_logger("openai_service")


class OpenAIService:
    """GPT-4.1 Mini tabanlı chat + vision servisi."""

    def __init__(self, api_key: str, model: str = "gpt-4.1-mini"):
        self.client = openai.OpenAI(api_key=api_key)
        self.model = model

    # ── Chat (Metin Tabanlı) ──

    def chat(self, messages: list[dict], temperature: float = 1.0, max_tokens: int = 2000) -> str:
        """
        OpenAI chat completion çağrısı.

        Args:
            messages: OpenAI format mesaj listesi [{"role": "...", "content": "..."}]
            temperature: Yaratıcılık seviyesi (geçmişe dönük uyumluluk için tutuldu)
            max_tokens: Maximum yanıt uzunluğu

        Returns:
            str: Modelin yanıtı
        """
        try:
            # temperature parametresi gönderilmiyor (model uyumu)
            # Çok kısa max_tokens'da boş content döndürebilir — minimum 100
            effective_max_tokens = max(max_tokens, 100)
            create_kwargs = {
                "model": self.model,
                "messages": messages,
                "max_completion_tokens": effective_max_tokens,
            }
            # Bazen boş content döndürebiliyor — retry mekanizması
            content = ""
            for attempt in range(3):
                response = self.client.chat.completions.create(**create_kwargs)
                content = response.choices[0].message.content or ""
                if content.strip():
                    break
                log.warning(f"OpenAI boş content döndürdü (deneme {attempt+1}/3)")
                if attempt < 2:
                    import time
                    time.sleep(0.5)  # Kısa bekleme — rate limit / cache sorunlarını aşmak için

            if not content.strip():
                log.error("OpenAI 3 denemede de boş content döndürdü (chat)")
                raise RuntimeError("OpenAI API 3 denemede de boş yanıt döndürdü. Lütfen tekrar deneyin.")

            log.info(f"Chat yanıt alındı — {len(content)} karakter, "
                     f"tokens: {response.usage.total_tokens}")
            return content

        except openai.RateLimitError:
            log.error("OpenAI rate limit aşıldı!", exc_info=True)
            raise
        except openai.APIError as e:
            log.error(f"OpenAI API hatası: {e}", exc_info=True)
            raise

    def chat_with_tools(self, messages: list[dict], tools: list[dict], max_tokens: int = 1500):
        """
        OpenAI chat completion çağrısı, ancak yetki (tools) listesiyle.
        Agent mimarisi için. 3 deneme ile retry yapar.
        """
        import time as _retry_time
        effective_max_tokens = max(max_tokens, 100)
        last_error = None

        for attempt in range(1, 4):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    max_completion_tokens=effective_max_tokens,
                )
                log.info(f"Chat (Tools) yanıt alındı — tokens: {response.usage.total_tokens}")
                return response.choices[0].message
            except openai.RateLimitError:
                log.warning(f"Tools API rate limit (deneme {attempt}/3)")
                last_error = "RateLimitError"
                if attempt < 3:
                    _retry_time.sleep(2 ** attempt)
            except openai.APIError as e:
                log.warning(f"Tools API hatası (deneme {attempt}/3): {e}")
                last_error = e
                if attempt < 3:
                    _retry_time.sleep(2 ** attempt)
            except Exception as e:
                log.error(f"OpenAI tools API beklenmeyen hata: {e}", exc_info=True)
                raise

        log.error(f"chat_with_tools 3 denemede başarısız: {last_error}")
        raise openai.APIError(f"3 denemede başarısız: {last_error}")

    # ── Görsel URL Validasyonu ──

    # OpenAI Vision API desteklenen formatlar
    SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    UNSUPPORTED_IMAGE_EXTENSIONS = {".avif", ".svg", ".bmp", ".tiff", ".tif", ".ico", ".heic", ".heif"}

    @staticmethod
    def _validate_image_url(image_url: str) -> bool:
        """
        Görsel URL'nin OpenAI Vision API tarafından desteklenip desteklenmediğini kontrol eder.
        
        CDN URL'lerinde query parametreleri olabilir — sadece path kısmına bakılır.
        Uzantısı belirsizse (örn: CDN hash URL) kabul edilir — OpenAI API'nin kendi
        validasyonuna bırakılır.
        """
        from urllib.parse import urlparse
        try:
            parsed = urlparse(image_url)
            path = parsed.path.lower()
            
            # Bilinen desteklenmeyen formatları reddet
            for ext in OpenAIService.UNSUPPORTED_IMAGE_EXTENSIONS:
                if path.endswith(ext):
                    return False
            
            return True
        except Exception:
            return False

    # ── Vision (Görsel Analiz) ──

    def analyze_image(self, image_url: str, prompt: str, max_tokens: int = 1500) -> str:
        """
        Ürün görselini GPT-4.1 Mini Vision ile analiz eder.

        Args:
            image_url: Public erişimli görsel URL'i
            prompt: Analiz talimatı

        Returns:
            str: Modelin görsel analiz yanıtı

        Raises:
            ValueError: Desteklenmeyen görsel formatı
        """
        # URL format validasyonu — desteklenmeyen formatları erken reddet
        if not self._validate_image_url(image_url):
            raise ValueError(
                f"Desteklenmeyen görsel formatı: {image_url[:80]}... "
                f"OpenAI Vision API sadece PNG, JPEG, GIF ve WebP destekler."
            )

        try:
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": image_url, "detail": "high"},
                        },
                    ],
                }
            ]

            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_completion_tokens=max_tokens,
            )
            content = response.choices[0].message.content
            if not content:
                log.warning("Vision API boş yanıt döndü — retry")
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_completion_tokens=max_tokens,
                )
                content = response.choices[0].message.content
                if not content:
                    raise RuntimeError("Vision API 2 denemede de boş yanıt döndü")
            log.info(f"Vision analiz tamamlandı — {len(content)} karakter")
            return content

        except ValueError:
            raise  # URL validasyon hatası — tekrar fırlatma
        except Exception:
            log.error("Görsel analiz hatası", exc_info=True)
            raise

    def analyze_image_bytes(self, image_bytes: bytes, mime_type: str, prompt: str,
                            max_tokens: int = 1500) -> str:
        """
        Telegram'dan gelen raw image bytes'ı analiz eder.
        Base64 encode ederek Vision API'ye gönderir.

        Args:
            image_bytes: Ham görsel verisi
            mime_type: MIME tipi (image/jpeg, image/png vb.)
            prompt: Analiz talimatı

        Returns:
            str: Modelin yanıtı
        """
        try:
            b64 = base64.b64encode(image_bytes).decode("utf-8")
            data_url = f"data:{mime_type};base64,{b64}"

            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url, "detail": "high"},
                        },
                    ],
                }
            ]

            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_completion_tokens=max_tokens,
            )
            content = response.choices[0].message.content
            if not content:
                log.warning("Image bytes API boş yanıt döndü — retry")
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_completion_tokens=max_tokens,
                )
                content = response.choices[0].message.content
                if not content:
                    raise RuntimeError("Image bytes API 2 denemede de boş yanıt döndü")
            log.info(f"Image bytes analiz tamamlandı — {len(content)} karakter")
            return content

        except Exception:
            log.error("Image bytes analiz hatası", exc_info=True)
            raise

    def select_best_product_image(self, image_urls: list[str]) -> str | None:
        """
        Verilen URL havuzu içinden ürünü en net gösteren tek bir fotoğrafı seçer.
        Structured JSON output kullanır (index-based).

        Args:
            image_urls: Ürün görsellerinin public URL'leri (max 5-10 önerilir)

        Returns:
            str | None: Seçilen en iyi görsel URL'i veya URL listesi boşsa None
        """
        valid_urls = [url for url in image_urls if self._validate_image_url(url)]
        if not valid_urls:
            return None

        if len(valid_urls) == 1:
            return valid_urls[0]

        try:
            # URL'leri numaralı liste olarak sun
            url_list = "\n".join([f"{i+1}. {url}" for i, url in enumerate(valid_urls[:10])])

            content_list = [
                {
                    "type": "text",
                    "text": (
                        "Aşağıdaki ürün fotoğraflarını incele. Bir e-ticaret reklamı üretileceği "
                        "için, ürünü en net, reklama en uygun ve yüksek kalitede gösteren "
                        "TEK BİR fotoğrafı seç.\n\n"
                        f"URL Listesi:\n{url_list}\n\n"
                        'JSON formatında yanıt ver: {"selected_index": <seçtiğin '
                        "numaralı URL'nin indeksi (1'den başlar)>}"
                    ),
                }
            ]
            for url in valid_urls[:10]:
                content_list.append({
                    "type": "image_url",
                    "image_url": {"url": url, "detail": "low"},
                })

            messages = [{"role": "user", "content": content_list}]

            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_completion_tokens=100,
                response_format={"type": "json_object"},
            )

            content = response.choices[0].message.content
            if not content:
                log.warning("Görsel seçimi boş yanıt — fallback")
                return valid_urls[0]

            import json
            result = json.loads(content)
            selected_idx = result.get("selected_index", 1)

            # Bounds check
            if isinstance(selected_idx, int) and 1 <= selected_idx <= len(valid_urls):
                selected_url = valid_urls[selected_idx - 1]
                log.info(f"OpenAI en iyi görseli seçti (structured): {selected_url[:60]}...")
                return selected_url

            log.warning(f"Geçersiz selected_index: {selected_idx}, fallback")
            return valid_urls[0]

        except Exception:
            log.error("Görsel seçme hatası", exc_info=True)
            return valid_urls[0]

    # ── JSON Çıktı ──

    def chat_json(self, messages: list[dict], temperature: float = 1.0,
                  max_tokens: int = 3000) -> dict:
        """
        Chat completion çağrısı — JSON response_format ile.

        Returns:
            dict: Parse edilmiş JSON yanıt
        """
        try:
            # temperature parametresi gönderilmiyor (model uyumu)
            # Çok kısa max_tokens'da boş content döndürebilir — minimum 200
            effective_max_tokens = max(max_tokens, 200)
            create_kwargs = {
                "model": self.model,
                "messages": messages,
                "max_completion_tokens": effective_max_tokens,
                "response_format": {"type": "json_object"},
            }
            # Bazen boş content döndürebiliyor — retry mekanizması
            content = ""
            for attempt in range(3):
                response = self.client.chat.completions.create(**create_kwargs)
                content = response.choices[0].message.content or ""
                if content.strip():
                    break
                log.warning(f"OpenAI JSON boş content döndürdü (deneme {attempt+1}/3)")
                if attempt < 2:
                    import time
                    time.sleep(0.5)
            log.info(f"JSON yanıt alındı — tokens: {response.usage.total_tokens}")
            if not content.strip():
                log.error("OpenAI JSON 3 denemede de boş content döndürdü")
                raise RuntimeError("OpenAI API 3 denemede de boş JSON yanıt döndürdü.")
            return json.loads(content)

        except json.JSONDecodeError:
            # GPT bazen JSON'u markdown code fence içinde döndürebilir
            # ```json {...} ``` → JSON bloğu çıkart
            import re
            match = re.search(r'\{[\s\S]*\}', content)
            if match:
                try:
                    recovered = json.loads(match.group())
                    log.warning("JSON markdown code fence'den kurtarıldı")
                    return recovered
                except json.JSONDecodeError as decode_error:
                    log.warning("JSON parsing failed, falling back", exc_info=True)
            log.error("OpenAI JSON parse hatası (kurtarılamadı)", exc_info=True)
            raise
        except Exception:
            log.error("OpenAI JSON chat hatası", exc_info=True)
            raise
