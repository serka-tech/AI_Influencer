"""
Retry Utility — Merkezi Yeniden Deneme Mekanizması
=====================================================
Tüm API çağrıları için exponential backoff ile retry.
Geçici hatalar (timeout, 429, 5xx) otomatik yeniden denenir.
Kalıcı hatalar (401, 403, 404) anında fırlatılır.
"""

import time
import functools
from typing import Optional

import requests

from logger import get_logger

log = get_logger("retry")

# Yeniden denenecek HTTP status kodları
# NOT: 401 eklendi — ElevenLabs gibi servisler geçici 401 dönebiliyor
# (deploy sırasında env yavaş yüklenmesi, servis tarafı geçici auth hatası)
# NOT: 512 eklendi — Kie AI reverse proxy (CloudFlare/nginx) upstream aşırı yüklenme kodu
RETRYABLE_STATUS_CODES = {401, 408, 429, 500, 502, 503, 504, 512}

class RateLimitError(Exception):
    """
    Rate limit (HTTP 429) hatası için özel exception.

    HTTPError fırlatamayan servisler (örn: Firecrawl) bu exception'ı fırlatarak
    retry decorator'ünün rate-limit aware backoff (Retry-After) mantığından
    yararlanabilir.

    Attributes:
        retry_after: Sunucudan gelen Retry-After header değeri (saniye, opsiyonel)
    """
    def __init__(self, message: str = "Rate limit exceeded", retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


# Yeniden denenecek exception türleri
RETRYABLE_EXCEPTIONS = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
    ConnectionResetError,
    TimeoutError,
    OSError,  # Network unreachable vb.
    RateLimitError,
)


def retry_api_call(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    operation_name: str = "",
):
    """
    API çağrıları için retry decorator.

    Args:
        max_retries: Maximum deneme sayısı (ilk deneme dahil DEĞİL)
        base_delay: İlk bekleme süresi (saniye)
        max_delay: Maximum bekleme süresi (saniye)
        backoff_factor: Her denemede bekleme çarpanı
        operation_name: Log'larda görünecek operasyon adı

    Usage:
        @retry_api_call(max_retries=3, operation_name="Kie AI createTask")
        def create_task(self, payload):
            ...
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            op = operation_name or func.__qualname__
            last_exception = None

            for attempt in range(1, max_retries + 2):  # +2: ilk deneme + retry'lar
                try:
                    return func(*args, **kwargs)
                except requests.exceptions.HTTPError as e:
                    status = e.response.status_code if e.response is not None else 0
                    # Retryable kontrol: sabit liste VEYA genel 5xx aralığı (500-599)
                    is_retryable = status in RETRYABLE_STATUS_CODES or (500 <= status <= 599)
                    if not is_retryable:
                        # Kalıcı hata — retry yapma
                        raise
                    last_exception = e
                    if attempt <= max_retries:
                        delay = min(base_delay * (backoff_factor ** (attempt - 1)), max_delay)
                        # 429 rate limit → Retry-After header varsa onu kullan
                        if status == 429 and e.response is not None:
                            retry_after = e.response.headers.get("Retry-After")
                            if retry_after:
                                try:
                                    delay = min(float(retry_after), max_delay)
                                except ValueError as e:
                                    log.warning("Failed to parse Retry-After header as float", exc_info=True)
                        log.warning(
                            f"{op}: HTTP {status}, retry {attempt}/{max_retries} "
                            f"({delay:.1f}s sonra)"
                        )
                        time.sleep(delay)
                    else:
                        raise
                except RETRYABLE_EXCEPTIONS as e:
                    last_exception = e
                    if attempt <= max_retries:
                        delay = min(base_delay * (backoff_factor ** (attempt - 1)), max_delay)
                        # RateLimitError → retry_after attribute varsa onu kullan
                        retry_after = getattr(e, "retry_after", None)
                        if retry_after is not None:
                            try:
                                delay = min(float(retry_after), max_delay)
                            except (ValueError, TypeError):
                                log.warning("Failed to parse retry_after attribute as float", exc_info=True)
                        log.warning(
                            f"{op}: {type(e).__name__}, retry {attempt}/{max_retries} "
                            f"({delay:.1f}s sonra)"
                        )
                        time.sleep(delay)
                    else:
                        raise
                except Exception:
                    # Bilinmeyen/kalıcı hata — retry yapma
                    raise

            # Buraya düşmemeli ama güvenlik için
            if last_exception:
                raise last_exception

        return wrapper
    return decorator
