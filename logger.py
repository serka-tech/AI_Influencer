"""
eCom Reklam Otomasyonu — Standard Logger
=========================================
- INFO ve üzeri çalışır (level parametresi ile değiştirilebilir)
- Exception stack trace'leri korunur
- Tüm Railway projelerinde aynı format
"""

import logging
import sys


def get_logger(name, level=logging.INFO):
    """
    Antigravity V2 Standard Logger.

    Args:
        name: Logger adı (modül/servis ismi)
        level: Log seviyesi (varsayılan: INFO)
    """
    logger = logging.getLogger(name)

    # Birden fazla handler eklenmesini önle
    if not logger.handlers:
        logger.setLevel(level)
        handler = logging.StreamHandler(sys.stdout)

        # Format: 2026-04-11 19:30:21 - module_name - INFO - Mesaj
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


def log_cost(project_name: str, cost: float, details: str = "") -> None:
    """
    Antigravity V2 Merkezi Maliyet Loglayıcı.
    _knowledge/maliyet-takibi.md dosyasına satır ekler.
    
    Args:
        project_name: Maliyeti oluşturan proje (örn: AI_Influencer)
        cost: Harcanan tutar (USD vb.)
        details: Harcama detayı (örn: 5 sahne, 1200 kredi)
    """
    import os
    import datetime
    
    # Kök dizini bul (_knowledge klasörünün olduğu yer: Projeler klasörünün bir üstü)
    # logger.py konumu: Projeler/AI_Influencer/logger.py
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    knowledge_dir = os.path.join(base_dir, "_knowledge")
    file_path = os.path.join(knowledge_dir, "maliyet-takibi.md")
    
    # Klasör yoksa sessizce atla (veya oluştur)
    if not os.path.exists(knowledge_dir):
        return
        
    date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # Dosya yoksa başlık ekle
    if not os.path.exists(file_path):
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("# Merkezi Maliyet Takibi\n\n| Tarih | Proje | Tutar | Detay |\n|---|---|---|---|\n")
            
    with open(file_path, "a", encoding="utf-8") as f:
        # Markdown table satırı: | 2026-06-09 19:30 | AI_Influencer | $0.25 | 3 sahne video üretimi |
        f.write(f"| {date_str} | {project_name} | ${cost:.4f} | {details} |\n")
