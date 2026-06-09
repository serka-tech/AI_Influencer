# AI Influencer — Çok Sahneli Veo 3.1 Video Otomasyonu

> Telegram bot ile sabit bir AI influencer karakteri için çok sahneli "ön kamera POV"
> kısa video (Reels / TikTok / Shorts) üretir. Nano Banana Pro + Veo 3.1 + Replicate.

**Proje:** Antigravity Ecosystem
**Tip:** Telegram Bot (Worker — long polling)
**Kaynak:** n8n "SKOOL - Çoklu Sahne Veo 3.1" akışının kod tabanlı yeniden kurulumu

---

## 🎯 Ne Yapar?

Telegram'dan video konusu, dil ve sahne sayısı alır → GPT ile çok sahneli senaryo üretir
→ her sahne için Nano Banana Pro ile ilk kare görseli (karakter referansıyla) ve Veo 3.1
ile native sesli image-to-video üretir → sahneleri birleştirir → videoyu Telegram'a gönderir
→ onayınla TikTok / Instagram / YouTube'a yayınlar.

### Pipeline
0. **Mod seçimi** — `/baslat`: 📹 *Normal video* veya 📍 *Mekan tanıtımı*. Mekan tanıtımında bot önce mekanın **videosunu** ister (Telegram'dan yüklenir); video Kie dosya sunucusuna kaydedilir ve senaryo o mekana göre yazılır. (Karakteri videodaki mekana görsel olarak yerleştirme 3. faza planlıdır; bu fazda video toplanır + senaryo mekan odaklı kurulur.)
1. **Giriş** — tek girişte ya 🔗 mekan/ürün *linki*, ya ✍️ *elle konu/mekan bilgisi*, ya da 🤖 *"yok"*. Sonra dil ve sahne sayısı (2-6, her sahne ~8 sn).
2. **Konu çözümü** — link verilirse sayfa okunup tanıtım konusu çıkarılır; "yok" denirse LLM konu üretir (`core/topic_resolver.py`). Mekan modunda konu, karakter o mekandaymış gibi tanıtım yapacak biçimde yönlendirilir.
2b. **Keşif/araştırma modu (otomatik)** — konu "çok mekanlı liste" tipiyse (örn. *"Mersin'de denize girilecek koylar"*), bot bunu otomatik algılar (`core/place_research.py`): hafif scraping (DuckDuckGo, anahtarsız) ile **gerçek mekan isimlerini** bulur, her mekan için **referans görsel** toplar (Openverse → Wikimedia, anahtarsız/telifsiz). Her sahne bir mekana ayrılır; Melisa o mekanın ismini söyler ve mekanın referans görseli Nano Banana Pro'ya 2. referans olarak verilerek karakter o mekandaymış gibi üretilir. Bulunan mekanlar onay ekranında listelenir, düzenlenebilir.
3. **Senaryo + ONAY** — GPT (`gpt-5.1`) ile her sahneye `text_to_image_prompt` + `image_to_video_prompt`. Senaryo Telegram'da gösterilir ve onay istenir: ✅ Onayla / ✏️ Düzenle (serbest metinle revizyon) / 🔄 Yeniden üret. Üretim ancak onaydan sonra ilerler.
4. **Maliyet + ONAY** — senaryo onaylanınca tahmini Kie kredisi + yaklaşık USD + kalan bakiye gösterilir (`core/cost.py`) ve tekrar onay istenir: ✅ Onayla, üret / ❌ İptal.
5. **Görsel** — Nano Banana Pro, sabit karakter referansı + 9:16 (Reels ölçüsü).
6. **Video** — Veo 3.1 fast, image-to-video, native ses (ayrı seslendirme yok).
7. **Birleştirme** — Replicate `lucataco/video-merge` ile concat.
8. **Açıklama + Hashtag** — keşfet/Reels odaklı paylaşım açıklaması + 12-20 hashtag (`core/caption_generator.py`).
9. **Teslim + Onay** — video + açıklama Telegram'a, ✅ Yayınla / ❌ Yayınlama.
10. **Yayın** — Upload-Post ile TikTok/Instagram(Reels)/YouTube (opsiyonel).

### Maliyet (ölçülen, 1K/9:16)
- Nano Banana Pro görsel: **18 kredi**, Veo 3.1 fast 8 sn video: **312 kredi** → sahne başı **330 kredi**.
- Kredi→USD `KIE_CREDIT_TO_USD` env ile ayarlanır (varsayılan ~$0.00125/kredi).

---

## 🏗️ Mimari

```
AI_Influencer/
├── main.py                      ← Telegram bot + form + onay akışı
├── config.py                    ← Fail-fast env + sabit karakter
├── logger.py
├── requirements.txt
├── nixpacks.toml / railway.json ← Railway worker deploy
├── .env.example
├── prompts/
│   ├── senaryo_system_prompt.txt   ← n8n sistem promptu (birebir)
│   ├── senaryo_user_template.txt   ← girdi şablonu (token'lı)
│   └── karakter_konsept.txt        ← karakter konsepti (sen doldurursun)
├── core/
│   ├── scenario_engine.py       ← senaryo üretimi + JSON doğrulama + retry + Türkçe özet
│   ├── place_research.py        ← keşif modu: gerçek mekan araştırma (scraping) + referans görsel
│   └── production_pipeline.py   ← görsel→video→concat orkestratörü (sahne başına mekan referansı)
├── services/
│   ├── kie_api.py               ← Nano Banana Pro + Veo 3.1 (create/poll)
│   ├── replicate_service.py     ← concat_videos
│   ├── upload_post_service.py   ← çoklu platform yayını
│   ├── openai_service.py        ← chat_json
│   └── imgbb_service.py         ← (opsiyonel) lokal görsel → public URL
└── utils/retry.py               ← exponential backoff
```

---

## ⚙️ Environment Setup

1. **Sanal ortam + bağımlılıklar**
   ```bash
   cd Projeler/AI_Influencer
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **.env oluştur**
   ```bash
   cp .env.example .env
   ```
   Doldur: `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`, `KIE_API_KEY`, `REPLICATE_API_TOKEN`.
   (Antigravity'de `master.env`'den `/sifre-bagla` ile otomatik bağlanabilir.)

3. **Karakteri tanımla**
   - Referans görsel: `REFERENCE_IMAGE_URL` (varsayılan ImgBB URL'i hazır).
   - Konsept: `prompts/karakter_konsept.txt` dosyasını kendi karakterinle doldur
     (veya `CHARACTER_DETAILS` env'ine yaz).

4. **(Opsiyonel) Yayınlama**
   - `UPLOAD_POST_API_KEY` + `UPLOAD_POST_PROFILE` doldurulursa onay sonrası
     `PUBLISH_PLATFORMS` listesine yükler. Boşsa video sadece Telegram'a teslim edilir.

5. **Çalıştır**
   ```bash
   python main.py
   ```
   Telegram'da `/baslat`.

---

## 📝 Notlar
- Veo 3.1 videoyu kendi sesiyle üretir → ElevenLabs/TTS yok.
- Görsel/video başarısız üretimlerde Kie AI kredisi iade eder.
- Karakter referans görseli ve `karakter_konsept.txt` `.gitignore`'da — paylaşımda sızmaz.
- Deploy için: `/canli-yayina-al` skill ile Railway worker olarak 7/24 alınabilir.
