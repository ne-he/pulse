# DEPLOY — PULSE

Backend di **Hugging Face Spaces** (Docker Space, gratis tanpa kartu), dashboard di
**Vercel** (statis). **JANGAN commit secret apa pun**: `.env` sudah di-gitignore, dan
satu-satunya key opsional diisi lewat Settings Space, bukan lewat file.

> **Status pengujian.** Dockerfile dan `deploy/space_boot.py` belum pernah di-build di
> mesin ini karena Docker tidak terpasang di sini. Yang sudah diverifikasi: jalur file
> di dalam image, urutan boot, dan encode/decode `?api=` antara `index.html` dan
> dashboard. Build sungguhannya baru terjadi di HF. Kalau gagal, log build ada di tab
> **Logs** Space dan langkah 5 di bawah menjelaskan cara membacanya.

---

## Kenapa satu container, bukan compose

`docker-compose.yml` memecah PULSE jadi lima layanan: redis, ingestion, ml, agent, api.
Itu bentuk yang benar untuk lokal, tapi Docker Space cuma memberi **satu container dan
satu port**. Redis juga bukan layanan yang disediakan HF, jadi tidak ada yang bisa
dituju kalau bus-nya ditaruh di luar.

Jadi image yang sama dipakai dua cara. Compose menimpa `command:` per layanan seperti
sebelumnya, tidak ada yang berubah untuk pemakaian lokal. Space memakai `CMD` default,
yaitu [`deploy/space_boot.py`](../deploy/space_boot.py), yang menyalakan redis lebih
dulu, menunggu sampai redis benar-benar menjawab PING, baru melepas ketiga worker, lalu
menyerahkan proses utama ke uvicorn.

Urutan tunggu itu bukan hiasan. Ketiga worker membuka koneksi ke bus di baris pertama,
jadi kalau mereka start sebelum redis siap, ketiganya mati seketika dan Space-nya
kelihatan sehat padahal kosong: API menyala, dashboard berhasil connect, dan tidak ada
satu pun event yang mengalir.

```
Vercel (statis)          Hugging Face Space (Docker, port 7860)
Frontend_pulse/   ──►    redis ──► ingestion ──► ml ──► agent
  index.html             				 └──► api (REST + WebSocket)
  ?api=<url space>
```

---

## 1) Backend — Hugging Face Space

### a. Buat Space
1. [huggingface.co](https://huggingface.co) → login → **New → Space**.
2. Owner: `ne-he`. Space name: `pulse-backend`. **SDK: Docker** → template **Blank**.
   Visibility: Public → **Create Space**.

### b. Push kode
Space adalah repo git tersendiri, terpisah dari GitHub. Tambahkan sebagai remote kedua:

```bash
git -C pulsev2 remote add space https://huggingface.co/spaces/ne-he/pulse-backend
```

```bash
git -C pulsev2 push space main
```

Password-nya bukan password akun, tapi **token write** dari
[huggingface.co/settings/tokens](https://huggingface.co/settings/tokens). Kalau sudah
pernah `huggingface-cli login`, kredensialnya kepakai otomatis.

Build memakan waktu 4 sampai 8 menit. Statusnya berubah jadi **Running** kalau berhasil.

### c. Secret dan variable (Settings → Variables and secrets)

**Tidak ada yang wajib.** PULSE default-nya jalan tanpa satu key pun: mode replay, data
sintetik, dan agent memakai template deterministik. Space bisa langsung hidup begitu
build selesai.

Yang opsional:

| Key | Jenis | Kegunaan |
|---|---|---|
| `GEMINI_API_KEY` | Secret | Incident card ditulis Gemini, bukan template. Kosong tetap jalan. |
| `ANOMALY_THRESHOLD` | Variable | **Baca bagian 4 sebelum deploy.** Default 0.85 terlalu longgar. |

### d. Verifikasi
Buka `https://ne-he-pulse-backend.hf.space/health`, harus menjawab status ok. Lalu
`https://ne-he-pulse-backend.hf.space/predictions/latest`, harus berisi prediksi per
stasiun. Kalau `/health` hidup tapi `/predictions/latest` kosong terus, berarti worker
mati dan bukan API-nya yang bermasalah, lihat langkah 5.

---

## 2) Dashboard — Vercel

Dashboard-nya HTML statis, tidak ada build step.

1. Buka [`Frontend_pulse/index.html`](../Frontend_pulse/index.html), isi baris `BACKEND`
   dengan URL Space, tanpa garis miring di akhir:

   ```js
   var BACKEND = "https://ne-he-pulse-backend.hf.space";
   ```

   Cukup satu baris ini. URL WebSocket diturunkan sendiri oleh dashboard dari nilai
   tersebut, `https` jadi `wss`, jadi tidak ada tempat kedua yang perlu diubah.

2. Commit dan push ke GitHub.
3. Vercel → **Add New → Project** → import repo `ne-he/pulse`.
4. **Root Directory:** `Frontend_pulse`
   **Framework Preset:** Other
   Build Command dan Install Command: **kosongkan**
   Output Directory: `.`
5. Deploy. Domainnya berupa `https://pulse-xxxx.vercel.app`.

CORS tidak perlu diatur: `api/main.py` sudah `allow_origins=["*"]`.

---

## 3) Urutan dan verifikasi akhir

1. Space → **Running** → cek `/health`.
2. Isi `BACKEND` di `index.html` → push → Vercel deploy.
3. Buka domain Vercel. Yang harus terlihat dalam 10 detik pertama:
   - chart terisi dari REST backfill, lalu bergerak sendiri lewat WebSocket,
   - drift status muncul,
   - tombol demo **trigger spike** menghasilkan lonjakan di chart lalu incident card.
4. Kalau chart diam tapi tidak ada error di console browser, hampir pasti nilai
   `BACKEND` salah atau kesisipan garis miring di akhir.

---

## 4) Yang harus diputuskan sebelum dipamerkan

**Ambang anomali.** Dengan `ANOMALY_THRESHOLD=0.85`, benchmark di mesin ini menandai
**5.873 dari 10.080 event sebagai anomali, yaitu 58,3%**. Detektor yang menyala di
lebih dari separuh event bukan detektor, dan efeknya paling kelihatan justru di demo:
feed insiden banjir kartu, dan lonjakan sungguhan dari tombol spike tenggelam di
antaranya.

Ini keputusanmu, bukan keputusan yang pantas ditebak dari sini, karena yang menentukan
adalah seberapa jarang kamu mau menyebut sesuatu anomali. Cara mengukurnya:

```bash
cd pulsev2 && python scripts/bench.py
```

Angka `anomaly_rate` di `docs/bench.json` adalah yang harus turun. Target wajar untuk
demo ada di kisaran 2 sampai 5%. Naikkan `ANOMALY_THRESHOLD` sedikit demi sedikit
(0.90, 0.95, 0.98), jalankan ulang, lalu pasang nilai final sebagai **Variable** di
Space. Tidak perlu rebuild, Space restart sendiri waktu variable berubah.

---

## 5) Kalau build atau runtime gagal

Log ada di tab **Logs** Space, dan `deploy/space_boot.py` sengaja berisik supaya
penyebabnya kebaca langsung:

| Yang terlihat di log | Artinya |
|---|---|
| `redis tidak menjawab PING dalam 30 detik` | redis-server gagal start. Cek apakah langkah `apt-get install redis-server` di Dockerfile lolos waktu build. |
| `PERINGATAN: ml berhenti lebih awal (exit 1)` | Worker crash. Traceback-nya ada di baris log tepat sebelum peringatan ini. |
| `/health` ok tapi `/predictions/latest` kosong | API hidup, loop putus. Cari peringatan worker di atas. |
| Build gagal di `pip install` | Biasanya `evidently` atau `river`. Log build menyebut paketnya. |

---

## 6) Batasan yang perlu diketahui

- **Space gratis tidur** setelah sekitar 48 jam tanpa traffic. Request pertama sesudah
  tidur lambat karena cold start. Wajar, dan tidak perlu dijelaskan ke recruiter selama
  kamu membuka linknya sendiri beberapa menit sebelum meeting.
- **Filesystem ephemeral.** Registry model (`data/registry`) hilang tiap restart, jadi
  riwayat versi model dimulai dari nol lagi. Loop drift sampai retrain sampai model card
  tetap jalan dan tetap terlihat, cuma tidak terakumulasi lintas restart.
- **Data replay itu sintetik**, dibangkitkan `ingestion/gen_sample.py` waktu boot pertama
  karena `data/` di-gitignore. Ini disengaja, artinya Space hidup tanpa dataset dan tanpa
  key. Untuk data Jakarta sungguhan, set `INGEST_MODE=live` plus `OPENAQ_API_KEY` sebagai
  secret Space.
- **Satu container berarti satu titik mati.** Kalau containernya restart, seluruh loop
  restart. Untuk demo portofolio ini cukup. Untuk produksi, kembalikan pemisahan layanan
  seperti di `docker-compose.yml`.
