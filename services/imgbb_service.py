"""
ImgBB Service — Görsel Hosting
================================
Telegram'dan gelen görselleri ImgBB'ye yükler.
Kie AI ve diğer servislerin erişebileceği public URL döndürür.
"""

from __future__ import annotations

import base64
import ipaddress
import socket
from urllib.parse import urlparse

import requests

from logger import get_logger

log = get_logger("imgbb_service")

IMGBB_UPLOAD_URL = "https://api.imgbb.com/1/upload"
REQUEST_TIMEOUT = 30


def _is_safe_url(url: str) -> bool:
    """SSRF koruması — yalnızca public http(s) URL'lere izin ver.

    Private/loopback/link-local/multicast/reserved IP'leri reddeder.
    """
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False
        host = p.hostname
        if not host:
            return False
        ip = ipaddress.ip_address(socket.gethostbyname(host))
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
        ):
            return False
        return True
    except Exception:
        return False


class ImgBBService:
    """ImgBB görsel hosting servisi."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    def upload_image_bytes(self, image_bytes: bytes, name: str = "product") -> dict:
        """
        Ham image bytes'ı ImgBB'ye yükler.

        Args:
            image_bytes: Raw image data
            name: Görsel adı (opsiyonel)

        Returns:
            dict: {"url": "https://...", "delete_url": "https://...", "size": 123456}

        Raises:
            Exception: Yükleme başarısız olursa
        """
        try:
            b64 = base64.b64encode(image_bytes).decode("utf-8")

            payload = {
                "key": self.api_key,
                "image": b64,
                "name": name,
            }

            response = requests.post(
                IMGBB_UPLOAD_URL,
                data=payload,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()

            data = response.json()
            if not data.get("success"):
                raise ValueError(f"ImgBB upload failed: {data}")

            result = {
                "url": data["data"]["url"],
                "display_url": data["data"]["display_url"],
                "delete_url": data["data"]["delete_url"],
                "size": data["data"]["size"],
            }

            log.info(f"ImgBB upload başarılı: {result['url']} ({result['size']} bytes)")
            return result

        except requests.exceptions.Timeout:
            log.error("ImgBB upload timeout")
            raise
        except Exception:
            log.error("ImgBB upload hatası", exc_info=True)
            raise

    def upload_image_url(self, image_url: str, name: str = "product") -> dict:
        """
        URL'deki görseli ImgBB'ye yükler.

        Args:
            image_url: Kaynak görsel URL'i
            name: Görsel adı

        Returns:
            dict: {"url": "...", "display_url": "...", "delete_url": "...", "size": ...}
        """
        try:
            payload = {
                "key": self.api_key,
                "image": image_url,
                "name": name,
            }

            response = requests.post(
                IMGBB_UPLOAD_URL,
                data=payload,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()

            data = response.json()
            if not data.get("success"):
                raise ValueError(f"ImgBB URL upload failed: {data}")

            result = {
                "url": data["data"]["url"],
                "display_url": data["data"]["display_url"],
                "delete_url": data["data"]["delete_url"],
                "size": data["data"]["size"],
            }

            log.info(f"ImgBB URL upload başarılı: {result['url']}")
            return result

        except Exception:
            log.error("ImgBB URL upload hatası", exc_info=True)
            raise

    def rehost_image_url(self, image_url: str, name: str = "rehosted") -> dict:
        """
        URL'den görseli indirip ImgBB'ye yeniden yükler.
        
        Bu işlem:
        1. Format normalizasyonu sağlar (ImgBB JPEG/PNG'ye dönüştürür)
        2. Geçici/kısa ömürlü URL'leri kalıcı hale getirir
        3. OpenAI Vision API ile uyumsuz formatları (avif vb.) çözer
        
        Args:
            image_url: Kaynak görsel URL'i (herhangi format)
            name: Görsel adı
            
        Returns:
            dict: {"url": "...", "display_url": "...", "delete_url": "...", "size": ...}
            
        Raises:
            Exception: İndirme veya yükleme başarısız olursa
        """
        try:
            # SSRF koruması — internal/private IP'lere fetch yapılmasın
            if not _is_safe_url(image_url):
                raise ValueError("unsafe url")

            # Görseli indir
            response = requests.get(image_url, timeout=REQUEST_TIMEOUT, stream=True)
            response.raise_for_status()
            
            # Content-Type kontrolü
            content_type = response.headers.get("Content-Type", "")
            if not content_type.startswith("image/"):
                raise ValueError(f"URL bir görsel döndürmedi: Content-Type={content_type}")
            
            image_bytes = response.content
            if len(image_bytes) < 1000:
                raise ValueError(f"Görsel çok küçük ({len(image_bytes)} bytes) — geçersiz olabilir")
            
            # ImgBB'ye yükle (bytes olarak — format dönüştürme otomatik)
            result = self.upload_image_bytes(image_bytes, name)
            log.info(f"Görsel rehost edildi: {image_url[:60]}... → {result['url']}")
            return result
            
        except Exception:
            log.error(f"Görsel rehost hatası: {image_url[:80]}", exc_info=True)
            raise
