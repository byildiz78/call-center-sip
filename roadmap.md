# AI Call Center - Roadmap & Proje Planı

## Proje Amacı
Robotpos destek hattına gelen telefon aramalarını AI sesli asistan ile karşılayıp, müşteriden bilgi toplayarak (ad, işletme adı, sorun tanımı) otomatik destek ticket'ı oluşturan bir çağrı merkezi sistemi.

## Mevcut Durum
- ✅ Gemini Live sesli asistan webchat çalışıyor (`server_gemini.py`, port 8081)
- ✅ OpenAI Realtime sesli asistan webchat çalışıyor (`server.py`, port 8080)
- Performans karşılaştırması sonucu **Gemini Live** tercih edildi

## Teknik Bilgiler

### API Anahtarları (.env)
- `OPENAI_API_KEY` - OpenAI Realtime API
- `GEMINI_API_KEY` - Gemini Live API (AIzaSyBGyaVJgsCEpuG5VY4aohBJw9x8xlzb_D4)

### Gemini Live API
- Model: `gemini-2.5-flash-native-audio-latest`
- WebSocket: `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=...`
- Input: PCM 16kHz 16-bit mono (base64)
- Output: PCM 24kHz 16-bit mono (base64)
- Ses: Kore (kadın sesi)

### SIP Trunk
- Sağlayıcı: **Verimor**
- Yönlendirme: Verimor panelinden istenen numara için hedef SIP adresi tanımlanıyor
- Örnek format: `<sunucu_ip>:5060`

### Ticket Sistemi
- Kullanıcının kendi REST API'si (endpoint detayları sonra verilecek)
- Webhook payload: caller_number, duration, summary, transcript, recording_url, timestamp, call_sid

### AI Agent System Prompt
```
## Identity
As a call center assistant, your primary duty is to obtain certain information from the caller by following the steps below in order. Do not repeat the information you obtain back to the caller. Focus only on gathering the necessary information. Keep the conversation as short as possible and stay focused on collecting the caller's details. Your manner of speaking should be fluent. Our company's name is Robotpos, and we are a very large firm that provides POS solutions for restaurants. Because our customers are restaurants, it is quite normal for business names to include terms such as "Köfteci," "Dönerci," "Kahveci," etc.; do not wait long between the questions you ask, adhere to the text in quotation marks below, and do not use the customer's name after saying thank you. Do not thank them after receiving the problem description.
Verdiğim Metnin dışında başka bir kelime cümle söyleme.

## Response Guardrails
Karşılama mesajında müşterinin adını aldıktan hemen sonra
Arayan kişi eğer sadece adını söylerse, ona bey yada hanım diye hitap etme, "işletmenizin adını alabilir miyim ?" diye sor sadece. ses erkek sesi ise bey, kadın sesi ise hanım diye hitap et.
İşletme Adını Alma:
"{{Müşteri Adı}} Bey/Hanım işletmenizin adını alabilir miyim?"

Sorun Tanımını Alma:
"Teşekkür ederim, lütfen yaşadığınız sorunu veya talebinizi kısaca anlatır mısınız?"

"Destek talebinizi sistemimize kayıt ettim, size yardımcı olabilecek uzman destek personelimiz kısa süre içinde sizinle iletişime geçecek, Dilerseniz esemes ile gelen bağlantıya tıklayarak WhatsApp destek hattımız ile doğrudan iletişime geçebilirsiniz, İyi günler dilerim."
{{end Call}}

## Important Instructions
- Always speak in Turkish
- Start the conversation by greeting the caller: "Robotpos destek hattına hoş geldiniz, adınızı alabilir miyim?"
- Follow the steps strictly in order
- Keep responses short and natural
```

---

## Mimari

```
Telefon → Verimor SIP Trunk → jambonz SBC (:5060)
                                    ↓
                              jambonz Node.js App (:3000)
                              (listen verb → WebSocket)
                                    ↓
                              Python AI Bridge (:8081)
                              /jambonz-ws endpoint
                                    ↓
                              Gemini Live API (wss://...)

                              SQLite DB (/data/calls.db)
                              Recordings (/recordings/)
                              Admin Panel (:8081/admin)
```

---

## Dosya Yapısı

```
voiceagentopenai/
├── .env
├── requirements.txt
├── server_gemini.py              # mevcut browser webchat (dokunma)
├── server.py                     # mevcut OpenAI webchat (dokunma)
├── static/
│   ├── index.html                # OpenAI webchat UI
│   └── gemini.html               # Gemini webchat UI
│
├── bridge/                       # AI Bridge Server
│   ├── __init__.py
│   ├── main.py                   # FastAPI entry point
│   ├── config.py                 # .env yükleme
│   ├── gemini_session.py         # Gemini Live WebSocket session (class)
│   ├── jambonz_handler.py        # jambonz WebSocket endpoint
│   ├── audio_utils.py            # Sample rate conversion (8k/16k ↔ 24k)
│   ├── recorder.py               # WAV kayıt modülü
│   ├── ticket.py                 # Arama sonu ticket webhook
│   ├── db.py                     # SQLite (aiosqlite) veritabanı
│   ├── models.py                 # CallRecord vb.
│   └── admin/
│       ├── routes.py             # Admin API routes
│       └── static/
│           └── index.html        # Admin panel SPA
│
├── jambonz-app/                  # jambonz call handler (Node.js)
│   ├── package.json
│   ├── index.js
│   └── .env
│
├── scripts/
│   └── install-jambonz.sh
│
├── recordings/                   # Ses kayıtları
│   └── YYYY-MM-DD/
│       └── {call_sid}_{timestamp}.wav
│
└── roadmap.md                    # Bu dosya
```

---

## Uygulama Adımları

### Adım 1: Python AI Bridge (`bridge/`) ✅ Tamamlandı
> Mevcut server_gemini.py'deki Gemini WebSocket proxy mantığını modüler class yapısına çevir

- [x] 1.1 `bridge/config.py` - Yapılandırma yönetimi
- [x] 1.2 `bridge/gemini_session.py` - Gemini Live oturum sınıfı
  - `connect()`, `send_audio(base64)`, `receive()` generator, `close()`
  - Her arama kendi bağımsız WebSocket bağlantısına sahip
- [x] 1.3 `bridge/audio_utils.py` - Ses dönüşümleri
  - `resample_24k_to_16k()` - Gemini çıktısını jambonz'a göndermek için
  - `resample_8k_to_16k()` - G.711 codec fallback
- [x] 1.4 `bridge/recorder.py` - Stereo WAV kayıt
  - Caller = sol kanal, Agent = sağ kanal
  - `/recordings/YYYY-MM-DD/{call_sid}_{timestamp}.wav`
- [x] 1.5 `bridge/db.py` - SQLite veritabanı (aiosqlite, WAL mode)
  - calls tablosu: call_sid, caller_number, start/end_time, duration, status, transcript (JSON), summary, recording_path, ticket_id
- [x] 1.6 `bridge/jambonz_handler.py` - jambonz WebSocket endpoint (KRİTİK)
  - Subprotocol: `audio.jambonz.org`
  - İlk mesaj: `session:new` JSON → call_sid, from
  - Sonraki: raw L16 PCM binary frames (16kHz)
  - İki yönlü köprü: jambonz ↔ Gemini
  - Eşzamanlı kayıt ve transcript yazma
- [x] 1.7 `bridge/ticket.py` - Arama sonu webhook
  - POST: caller_number, duration, summary, transcript, recording_url, timestamp, call_sid
- [x] 1.8 `bridge/admin/routes.py` - Admin API
  - GET /api/calls (pagination, filtre)
  - GET /api/calls/{call_sid} (detay + transcript)
  - GET /api/recordings/{call_sid} (WAV stream)
  - GET /api/stats
  - GET /api/active (aktif aramalar)
- [x] 1.9 `bridge/admin/static/index.html` - Admin Panel
  - Arama listesi, detay modal, ses player, filtreler
  - İstatistik kartları, canlı aktif arama sayısı
- [x] 1.10 `bridge/main.py` - FastAPI app birleştirme
  - /jambonz-ws, /ws (browser test), /admin, /api/* tümü mount edildi

### Adım 2: jambonz Node.js App (`jambonz-app/`) ✅ Tamamlandı
- [x] 2.1 `index.js` - @jambonz/node-client-ws ile call handler
  - Gelen arama → listen verb → ws://127.0.0.1:8081/jambonz-ws
  - bidirectionalAudio: streaming, 16kHz
  - Metadata: callSid, callerNumber

### Adım 3: jambonz Kurulum (VPS) ⬜ Bekliyor
- [ ] 3.1 VPS hazırlığı: Ubuntu 22.04, 4+ CPU, 8GB RAM, statik IP
- [ ] 3.2 jambonz-mini bare metal kurulumu
- [ ] 3.3 Portal yapılandırması: Verimor SIP trunk, application, routing
- [ ] 3.4 Verimor'da hedef SIP: `<VPS_IP>:5060`

### Adım 4: Entegrasyon & Test ⬜ Bekliyor
- [ ] 4.1 Browser client ile bridge test
- [ ] 4.2 jambonz → bridge → Gemini akış testi
- [ ] 4.3 Kayıt, transcript, ticket webhook doğrulama
- [ ] 4.4 5+ eşzamanlı arama yük testi

---

## Concurrency Notları
- FastAPI async: Tek worker 20+ eşzamanlı WebSocket yönetir
- Her arama ~19 MB bellek (10 dk), 20 arama = ~380 MB
- SQLite WAL mode: eşzamanlı okuma/yazma sorunsuz
- Gemini API: 20 eşzamanlı WebSocket - API kotası kontrol edilmeli
- jambonz-mini: 50-75 eşzamanlı arama kapasitesi (yeterli)

---

## Önemli Teknik Detaylar

### jambonz listen verb protokolü
- WebSocket subprotocol: `audio.jambonz.org`
- İlk mesaj: JSON text frame `{ "type": "session:new", "callSid": "...", "from": "..." }`
- Ses: binary frames, raw L16 PCM 16-bit
- Bidirectional: hem gelen hem giden ses aynı WebSocket üzerinden

### Gemini ↔ jambonz ses formatı farkı
- Gemini input: base64 encoded PCM 16kHz
- Gemini output: base64 encoded PCM 24kHz
- jambonz: raw binary PCM 16kHz (veya 8kHz)
- Bridge'de dönüşüm gerekli: decode/encode + resample

### server_gemini.py'den yeniden kullanılacak kod
- Gemini WebSocket bağlantı kurulumu (satır 44-69)
- Setup message yapısı (model, voice, system instruction)
- Audio forwarding mantığı (satır 88-100)
- Response parsing (serverContent, modelTurn, parts) (satır 108-145)

---
> 🧭 Hıdır was here — 2026-03-18
