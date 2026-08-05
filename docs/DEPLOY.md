# DEPLOY PULSE

Backend di **Render** (Free Web Service, Docker, tanpa kartu kredit), dashboard di
**Vercel** (statis). **JANGAN commit secret apa pun**: `.env` sudah di-gitignore, dan
satu-satunya key opsional diisi lewat Environment Render, bukan lewat file.

> **Kenapa bukan Hugging Face Spaces lagi.** Sejak sekitar 8 Juli 2026, HF memindahkan
> Docker Space ke belakang paywall: pesan errornya berbunyi bahwa Static Space gratis
> untuk semua orang, tapi hosting Gradio dan Docker Space di cpu-basic butuh langganan
> PRO. Tidak ada pengumuman resmi dan halaman pricing-nya belum diperbarui, jadi kalau
> suatu saat kebijakan ini berubah, `README.md` masih menyimpan frontmatter `sdk: docker`
> dan langkahnya tinggal dibalik lagi. Untuk sekarang, Render yang dipakai.

> **Status pengujian.** Dockerfile dan `deploy/space_boot.py` belum pernah di-build di
> mesin ini karena Docker tidak terpasang di sini. Yang sudah diverifikasi: jalur file di
> dalam image, urutan boot, penggunaan RAM tiap proses (bagian 2), dan encode/decode
> `?api=` antara `index.html` dan dashboard. Build sungguhannya baru terjadi di Render.
> Kalau gagal, bagian 6 menjelaskan cara membaca lognya.

---

## 1) Kenapa satu container, bukan compose

`docker-compose.yml` memecah PULSE jadi lima layanan: redis, ingestion, ml, agent, api.
Itu bentuk yang benar untuk lokal, tapi **Web Service adalah satu-satunya jenis layanan
yang gratis di Render**. Background Worker dan Key Value (Redis terkelola) keduanya
berbayar, jadi tidak ada tempat untuk menaruh keempat proses lain maupun bus-nya.

Jadi image yang sama dipakai dua cara. Compose menimpa `command:` per layanan seperti
sebelumnya, tidak ada yang berubah untuk pemakaian lokal. Render memakai `CMD` default,
yaitu [`deploy/space_boot.py`](../deploy/space_boot.py), yang menyalakan redis lebih
dulu, menunggu sampai redis benar-benar menjawab PING, baru melepas ketiga worker, lalu
menyerahkan proses utama ke uvicorn.

Urutan tunggu itu bukan hiasan. Ketiga worker membuka koneksi ke bus di baris pertama,
jadi kalau mereka start sebelum redis siap, ketiganya mati seketika dan servicenya
kelihatan sehat padahal kosong: API menyala, dashboard berhasil connect, dan tidak ada
satu pun event yang mengalir.

```
Vercel (statis)          Render Free Web Service (Docker, satu container)
Frontend_pulse/   ──►    redis ──► ingestion ──► ml ──► agent
  index.html             				 └──► api (REST + WebSocket)
  ?api=<url render>
```

---

## 2) Anggaran RAM: 383 MB dari 512 MB

Instance gratis Render memberi **512 MB RAM dan 0.1 CPU** untuk seluruh container, dan
container ini menjalankan lima proses sekaligus. Itu cukup sempit untuk ditebak, jadi
diukur langsung (`RSS` tiap proses setelah modulnya diimpor penuh):

| Proses | RSS |
|---|---|
| `ingestion.replay` | 88,8 MB |
| `ml.online.consumer` (sudah termasuk evidently) | 166,5 MB |
| `agent.incident` | 68,1 MB |
| `api.main` | 49,4 MB |
| `redis-server` (perkiraan) | ~10 MB |
| **Total** | **~383 MB** |

Sisa ruangnya sekitar 130 MB untuk buffer drift, state model river, dan lonjakan
sementara. **Muat, tapi tidak lega.** Dua catatan jujur soal angka ini: diukur di Windows
dengan Python 3.12, sedangkan image-nya Linux Python 3.11 yang biasanya sedikit lebih
hemat, jadi anggap ini batas atas yang wajar. Kalau ternyata kena OOM, yang paling besar
dan paling gampang dilepas adalah evidently: `check_drift` sudah punya fallback PSI murni
pandas dan seluruh 27 test tetap hijau tanpa evidently, jadi mencopotnya dari
`requirements.txt` memangkas paling banyak RAM dengan risiko paling kecil.

**Soal 0.1 CPU.** Default `REPLAY_SPEED=600` menghasilkan sekitar satu frame per detik,
yaitu lima event per detik untuk lima stasiun. Benchmark di mesin ini mencapai 269,7
event/detik dengan CPU penuh, jadi lima event/detik di sepersepuluh CPU masih masuk akal.
Kalau dashboard-nya kelihatan tersendat, turunkan `REPLAY_SPEED` ke `200` lewat
Environment Render. Tidak perlu rebuild.

---

## 3) Backend: Render

### a. Buat service
1. [render.com](https://render.com) → **Get Started** → login pakai GitHub. Tidak perlu
   kartu kredit.
2. **New → Web Service** → **Build and deploy from a Git repository** → pilih repo
   `ne-he/pulse`. Kalau reponya belum kelihatan, klik **Configure account** dan beri
   Render akses ke repo itu.

### b. Setelan
| Kolom | Nilai |
|---|---|
| Name | `pulse-backend` |
| Language / Runtime | **Docker** (terdeteksi otomatis dari `Dockerfile` di root) |
| Branch | `main` |
| Instance Type | **Free** |
| Health Check Path | `/health` |

Root Directory dikosongkan saja, Dockerfile-nya memang di root repo.

Port tidak perlu diisi. Render menyuntik `PORT` sendiri, dan `space_boot.py` membacanya
lewat `os.environ.get("PORT", "7860")`, jadi uvicorn otomatis listen di port yang benar.

### c. Environment (opsional, semuanya boleh kosong)
PULSE default-nya jalan tanpa satu key pun: mode replay, data sintetik, dan agent memakai
template deterministik.

| Key | Kegunaan |
|---|---|
| `GEMINI_API_KEY` | Incident card ditulis Gemini, bukan template. Kosong tetap jalan. |
| `ANOMALY_THRESHOLD` | **Baca bagian 5 sebelum dipamerkan.** Default 0.85 terlalu longgar. |
| `REPLAY_SPEED` | Turunkan ke `200` kalau streamnya tersendat di 0.1 CPU. |

### d. Deploy dan verifikasi
Klik **Create Web Service**. Build pertama 5 sampai 10 menit (`pip install` river,
evidently, pandas). Statusnya berubah jadi **Live** kalau berhasil.

Cek `https://pulse-backend-xxxx.onrender.com/health`, harus menjawab status ok. Lalu
`/predictions/latest`, harus berisi prediksi per stasiun. Kalau `/health` hidup tapi
`/predictions/latest` kosong terus, berarti worker mati dan bukan API-nya yang bermasalah,
lihat bagian 6.

---

## 4) Dashboard: Vercel

Dashboard-nya HTML statis, tidak ada build step.

1. Buka [`Frontend_pulse/index.html`](../Frontend_pulse/index.html), isi baris `BACKEND`
   dengan URL Render, tanpa garis miring di akhir:

   ```js
   var BACKEND = "https://pulse-backend-xxxx.onrender.com";
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

### Urutan dan verifikasi akhir
1. Render → **Live** → cek `/health`.
2. Isi `BACKEND` di `index.html` → push → Vercel deploy.
3. Buka domain Vercel. Yang harus terlihat dalam 10 detik pertama:
   - chart terisi dari REST backfill, lalu bergerak sendiri lewat WebSocket,
   - drift status muncul,
   - tombol demo **trigger spike** menghasilkan lonjakan di chart lalu incident card.
4. Kalau chart diam tapi tidak ada error di console browser, hampir pasti nilai `BACKEND`
   salah atau kesisipan garis miring di akhir.

---

## 5) Yang harus diputuskan sebelum dipamerkan

**Ambang anomali.** Dengan `ANOMALY_THRESHOLD=0.85`, benchmark di mesin ini menandai
**5.873 dari 10.080 event sebagai anomali, yaitu 58,3%**. Detektor yang menyala di lebih
dari separuh event bukan detektor, dan efeknya paling kelihatan justru di demo: feed
insiden banjir kartu, dan lonjakan sungguhan dari tombol spike tenggelam di antaranya.

Ini keputusanmu, bukan keputusan yang pantas ditebak dari sini, karena yang menentukan
adalah seberapa jarang kamu mau menyebut sesuatu anomali. Cara mengukurnya:

```bash
cd pulsev2 && python scripts/bench.py
```

Angka `anomaly_rate` di `docs/bench.json` adalah yang harus turun. Target wajar untuk demo
ada di kisaran 2 sampai 5%. Naikkan `ANOMALY_THRESHOLD` sedikit demi sedikit (0.90, 0.95,
0.98), jalankan ulang, lalu pasang nilai final sebagai Environment di Render. Tidak perlu
rebuild, service restart sendiri waktu environment berubah.

---

## 6) Kalau build atau runtime gagal

Log ada di tab **Logs** service Render, dan `deploy/space_boot.py` sengaja berisik supaya
penyebabnya kebaca langsung:

| Yang terlihat di log | Artinya |
|---|---|
| `redis tidak menjawab PING dalam 30 detik` | redis-server gagal start. Cek apakah langkah `apt-get install redis-server` di Dockerfile lolos waktu build. |
| `PERINGATAN: ml berhenti lebih awal (exit 1)` | Worker crash. Traceback-nya ada di baris log tepat sebelum peringatan ini. |
| `Exited with status 137` atau service restart terus tanpa traceback | **OOM.** 137 = SIGKILL dari kernel, bukan bug kode. Lihat bagian 2, buang evidently dari `requirements.txt`. |
| `/health` ok tapi `/predictions/latest` kosong | API hidup, loop putus. Cari peringatan worker di atas. |
| Build gagal di `pip install` | Biasanya `evidently` atau `river`. Log build menyebut paketnya. |

---

## 7) Batasan yang perlu diketahui

- **Service gratis tidur setelah 15 menit tanpa traffic**, dan bangunnya makan sekitar
  satu menit. Ini jauh lebih agresif daripada Space HF yang dulu tahan ~48 jam. Praktisnya:
  **buka linknya sendiri satu menit sebelum meeting**, jangan mengirim link lalu menyuruh
  orang klik saat itu juga. Kalau tidak, yang dia lihat pertama adalah halaman loading.
- **750 jam instance per bulan untuk SATU workspace, dibagi semua service.** Satu service
  hidup nonstop sebulan sudah memakan ~730 jam, jadi dua backend yang keduanya nonstop
  akan menembus kuota dan keduanya disuspend sampai bulan berikutnya. Karena semuanya
  tidur waktu tidak dipakai, ini aman untuk demo portofolio. Yang perlu dijaga: **tab
  dashboard yang dibiarkan terbuka menahan service tetap bangun lewat koneksi WebSocket**,
  jadi jangan ditinggal nyala semalaman.
- **Filesystem ephemeral.** Registry model (`data/registry`) hilang tiap restart, jadi
  riwayat versi model dimulai dari nol lagi. Loop drift sampai retrain sampai model card
  tetap jalan dan tetap terlihat, cuma tidak terakumulasi lintas restart.
- **Data replay itu sintetik**, dibangkitkan `ingestion/gen_sample.py` waktu boot pertama
  karena `data/` di-gitignore. Ini disengaja, artinya service hidup tanpa dataset dan tanpa
  key. Untuk data Jakarta sungguhan, set `INGEST_MODE=live` plus `OPENAQ_API_KEY`.
- **Satu container berarti satu titik mati.** Kalau containernya restart, seluruh loop
  restart. Untuk demo portofolio ini cukup. Untuk produksi, kembalikan pemisahan layanan
  seperti di `docker-compose.yml`.
