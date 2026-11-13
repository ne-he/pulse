# PULSE: sistem kualitas udara Jakarta yang terus belajar, memantau diri sendiri, dan mendokumentasikan dirinya setelah model naik produksi

**Sekilas**

| Aspek | Isi |
|---|---|
| Apa | Sistem streaming real-time untuk PM2.5 di 5 stasiun Jakarta. Model belajar dari setiap bacaan baru, mendeteksi lonjakan, memantau drift per stasiun, lalu otomatis membuat versi model baru beserta model card dan kartu insiden. |
| Untuk siapa | Rekruter dan engineer yang ingin melihat bukti kemampuan MLOps: apa yang terjadi pada model setelah deploy, bukan cuma melatihnya. |
| Status | Backend dan dashboard lengkap, jalan end-to-end di lokal. CI hijau, workflow terjadwal jalan harian. Sengaja tanpa demo publik. |
| Link live | Tidak ada, ini keputusan desain (lihat bagian 7). Bukti visual: GIF 20 detik di `docs/demo.gif` yang tampil di README. |
| Repo | https://github.com/ne-he/pulse |
| Teknologi utama | Python, river (online learning), Redis Streams, FastAPI + WebSocket, PSI drift detector (Evidently opsional), Gemini `gemini-2.5-flash` dengan fallback template, Docker Compose, GitHub Actions |
| Terakhir diperbarui | 11 November 2026 (commit `84b004a`, GIF demo di README) |

## 1. Projek ini tentang apa

Bayangkan penjaga warung kopi di pinggir jalan yang tiap sepuluh menit menebak berapa pembeli yang akan datang sejam lagi. Dia tidak sekolah statistik dulu selama sebulan lalu berhenti belajar. Setiap kali ada pembeli datang, dia langsung membandingkan tebakannya dengan kenyataan dan membetulkan perasaannya sedikit. Kalau tiba-tiba ada rombongan bus berhenti, dia sadar itu kejadian aneh dan bilang ke pemilik. Kalau pola jalanan berubah total, misalnya ada proyek galian yang bikin macet tiap hari, dia membuka halaman baru di buku catatannya dan menulis "mulai hari ini polanya beda, alasannya ini".

PULSE adalah penjaga warung itu, versi kualitas udara Jakarta. Ada lima stasiun (Jaksel, Jakpus, Jakbar, Jaktim, Jakut). Tiap stasiun punya model sendiri yang menebak PM2.5 satu jam ke depan, lengkap dengan rentang ketidakpastian. Model belajar dari setiap bacaan, satu per satu. Lonjakan aneh ditandai sebagai anomali dan diubah jadi kartu insiden berbahasa manusia. Pergeseran pola jangka panjang (drift) memicu pembuatan versi model baru plus dokumen penjelasnya.

Semua itu tampil di dashboard gelap ala ruang kontrol.

## 2. Masalah yang mau diselesaikan

Ada dua masalah, satu soal dunia nyata dan satu soal portfolio.

Masalah dunia nyata: data kualitas udara itu mengalir terus. Pertanyaan warga juga bukan "berapa rata-rata PM2.5 tahun lalu", tapi "aman tidak keluar rumah sejam lagi". Model yang dilatih sekali lalu dibekukan pelan-pelan jadi basi karena musim, lalu lintas, dan cuaca berubah.

Masalah portfolio: hampir semua projek ML junior berhenti di kalimat "saya melatih model dan akurasinya sekian persen". Pekerjaan sungguhan di industri justru dimulai setelah itu. Model perlu dipantau. Distribusi data bergeser. Versi model harus dicatat, dan dokumentasinya harus ikut berubah. Tagline PULSE di README memang "Jakarta Air Quality, after deploy". Projek ini sengaja menyasar celah itu.

## 3. Cara kerjanya, langkah demi langkah

Gambaran besarnya dulu. Semua komponen ngobrol lewat "papan pengumuman" bernama Redis Streams. Nama-nama stream-nya didefinisikan di satu tempat, `common/config.py` kelas `Streams`.

```
ingestion (replay/live) -> aq.events
aq.events -> ml/online/consumer.py (forecast, learn_one, anomaly, drift)
consumer -> aq.predictions -> api (WebSocket) -> dashboard chart
consumer -> aq.alerts -> agent (Gemini atau template) -> aq.incidents -> dashboard feed
consumer -> drift per stasiun -> retrain -> registry versi baru + model card baru
dashboard tombol SPIKE -> POST /control -> aq.control -> replay
```

### Perjalanan satu event sensor

Ikuti satu bacaan dari stasiun Jaksel.

1. **Data lahir di ingestion.** Mode default adalah replay. `ingestion/replay.py` membaca `data/sample_aq.csv`, data sintetis Jakarta buatan `ingestion/gen_sample.py`: 5 stasiun, 14 hari, resolusi 10 menit, total 10.080 baris, dengan pola jam sibuk pagi dan sore, ritme mingguan, efek angin, dan beberapa lonjakan. Satu "frame" adalah satu timestamp yang berisi 5 event (satu per stasiun). Dengan `REPLAY_SPEED=600`, kira-kira satu frame keluar per detik.
2. **Event dikemas dan diumumkan.** Tiap baris diubah jadi objek `AQEvent` (pydantic) berisi `pm25`, `pm10`, `no2`, `o3`, `temp`, `humidity`, `wind_speed`, lalu dipublish ke stream `aq.events`.
3. **Consumer menebak dulu, baru belajar.** `Engine.process` di `ml/online/consumer.py` mengambil model stasiun Jaksel (`StationModel` di `ml/online/model.py`). Urutannya penting: forecast dibuat dengan model saat ini, sebelum model melihat bacaan barunya. Model-nya SNARIMAX dari river (`p=2, d=1, q=1, m=1`) dengan fitur cuaca dan jam (sinus/kosinus). Forecast dibuat 6 langkah ke depan, artinya 60 menit. Pita ketidakpastiannya adalah forecast plus minus 1,96 kali standar deviasi residual terbaru.
4. **Detektor anomali menilai kejutan.** `AnomalyDetector` di `ml/online/anomaly.py` menghitung selisih bacaan sekarang dengan bacaan sebelumnya, lalu membaginya dengan sigma yang diestimasi dari MAD (median absolute deviation) 300 residual terakhir. Hasilnya `z`, lalu dipadatkan jadi `score = z / (1 + z)`. Ambang default 0,85 artinya lompatan minimal 5,67 sigma. Ada dua rem: 30 event pertama tidak boleh alarm (warm-up), dan bacaan dengan AQI di bawah 51 (kategori Good) tidak dianggap insiden.
5. **Belajar online yang sungguhan.** `model.observe(y, exog)` melakukan tiga hal: menilai forecast satu langkah yang dibuat tick sebelumnya (residualnya masuk ke hitungan MAE/RMSE live), memanggil `learn_one(y, x=exog)`, lalu menyimpan forecast satu langkah baru untuk dinilai di tick berikutnya. Inilah inti projek: bobot model berubah di setiap event, bukan lewat retrain batch terjadwal.
6. **Prediksi diumumkan.** Consumer membuat objek `Prediction` (PM2.5 sekarang, AQI, forecast, batas bawah dan atas, kategori kesehatan, tren naik/turun/datar, versi model, MAE/RMSE rolling, jumlah event yang sudah dipelajari) dan mempublishnya ke `aq.predictions`.
7. **Kalau anomali, alert dikirim.** Consumer mempublish `Alert` ke `aq.alerts` dengan konteks lengkap: nilai asal, besar lompatan, arah (naik atau turun), jumlah sigma, kecepatan angin, dan forecast.
8. **Agent menulis kartu insiden.** `agent/incident.py` membaca `aq.alerts`. Kartu template deterministik selalu disiapkan dulu. Kalau `GEMINI_API_KEY` ada, prompt dikirim ke `gemini-2.5-flash` dan teks Gemini dipakai. Kalau Gemini gagal 3 kali berturut-turut, agent berhenti mencoba sampai proses selesai. Field `generated_by` berisi `gemini` atau `template`, dan dashboard menampilkannya sebagai label. Kartu template juga sadar arah: penurunan tidak akan diberi judul "spike".
9. **API meneruskan ke browser.** `api/main.py` (FastAPI) membaca stream dan menyiarkan setiap pesan lewat WebSocket `/ws`. Klien baru langsung menerima satu `snapshot` supaya layar tidak kosong. Ada juga REST untuk riwayat, misalnya `/predictions/{id}/history`.
10. **Dashboard menggambar.** Dashboard di `Frontend_pulse/` membaca alamat backend dari parameter URL `?api=`. Kalau kosong, default-nya `http://localhost:8000`. Titik baru masuk ke chart, garis putus-putus forecast bergerak, pitanya melebar atau menyempit.

Tombol `[SPIKE]` di dashboard mengirim `POST /control` dengan aksi `trigger_spike`. Perintah itu masuk ke `aq.control`, dibaca replay, lalu replay menyuntikkan lonjakan yang memudar dengan faktor 0,88 selama 18 frame. Angka itu dipilih karena paling murah yang menghasilkan tepat satu alert (tabel ukurnya ada di komentar `ingestion/replay.py`).

### Perjalanan satu kejadian drift sampai model baru dipromosikan

1. **Jendela referensi diisi.** `Engine._track_drift` menyimpan 200 event pertama tiap stasiun sebagai referensi (`drift_window=200`). Empat fitur yang dipantau: `pm25`, `temp`, `humidity`, `wind_speed`.
2. **Jendela saat ini bergeser.** Setelah referensi penuh, event berikutnya masuk ke jendela "current" 200 event yang terus bergeser.
3. **Cek berkala.** Setiap 200 event, semua stasiun yang kedua jendelanya sudah penuh dicek lewat `check_drift_by_station` di `ml/monitoring/drift.py`.
4. **PSI per fitur, per stasiun.** Referensi dipotong jadi 10 bin berdasarkan kuantil, lalu PSI dihitung. Fitur dianggap drift kalau PSI di atas 0,2. Stasiun dianggap drift kalau minimal separuh fiturnya drift (`drift_threshold=0.5`, jadi 2 dari 4).
5. **Aturan pemicu: stasiun mana pun.** Kalau satu stasiun saja drift, retrain jalan. Alasannya ditulis panjang di docstring: biaya salah picu cuma satu baris registry dan satu file markdown, sedangkan biaya tidak memicu adalah model card yang diam-diam menggambarkan kondisi yang sudah tidak ada.
6. **Retrain = memotret state online.** `Engine._retrain` menghitung rata-rata MAE/RMSE semua stasiun, lalu `Registry.register` di `ml/registry/registry.py` menulis `vN.json` dan memperbarui `active.json`. Penting dipahami: model online sudah beradaptasi per event, jadi "retrain" di sini artinya membekukan state yang sudah beradaptasi jadi versi resmi yang terdokumentasi, bukan melatih ulang dari nol.
7. **Model card ditulis ulang otomatis.** `ml/modelcard/generate.py` membuat markdown berisi alasan promosi, metrik, tabel drift per fitur, tabel drift per stasiun, dan stasiun mana yang memicu.
8. **Drift juga dinarasikan.** Consumer mengirim alert bertipe `drift`, dan agent menulis kartu "Model retrained -> vN".
9. **Hanya stasiun yang bergeser yang di-reset.** Referensi stasiun yang drift diganti dengan jendela terbarunya. Stasiun yang tenang tetap dinilai terhadap referensi lamanya. Ini yang mencegah satu sensor rewel memicu retrain berulang untuk pergeseran yang sama.

## 4. Peta kode: folder dan file penting

| Path | Isinya apa |
|---|---|
| `common/config.py` | Semua setting dari env (12-factor), nama stream, model Gemini `gemini-2.5-flash` |
| `common/schemas.py` | Kontrak data pydantic: `AQEvent`, `Prediction`, `Alert`, `Incident`, daftar 5 stasiun |
| `common/redis_bus.py` | Pembungkus Redis Streams. `REDIS_URL=memory://` memakai fakeredis di dalam proses |
| `common/health.py` | Konversi PM2.5 ke US AQI dan kategori kesehatan beserta warnanya |
| `ingestion/gen_sample.py` | Generator data sintetis Jakarta (seed 42) |
| `ingestion/replay.py` | Memutar CSV seolah live, membaca perintah kontrol, menyuntik spike |
| `ingestion/producer.py` | Mode live: OpenAQ v3 (butuh key) dengan fallback Open-Meteo tanpa key |
| `ml/online/model.py` | `StationModel`: SNARIMAX river, pita ketidakpastian, MAE/RMSE live, guard divergensi |
| `ml/online/anomaly.py` | Detektor anomali berbasis kejutan forecast satu langkah (MAD) |
| `ml/online/consumer.py` | `Engine`: jantung sistem, termasuk loop drift ke retrain |
| `ml/monitoring/drift.py` | PSI in-house, jalur Evidently, dan agregasi per stasiun |
| `ml/registry/registry.py` | Registry versi model berbasis file JSON lokal |
| `ml/modelcard/generate.py` | Generator model card markdown |
| `ml/batch/baseline.py` | Baseline batch sebagai pembanding |
| `agent/incident.py`, `agent/prompts.py` | Agent kartu insiden (Gemini + template) dan teks prompt-nya |
| `api/main.py`, `api/ws.py` | REST, WebSocket, `POST /control`, dan penyajian dashboard dari origin yang sama |
| `Frontend_pulse/` | Dashboard dc-runtime (`index.html` mengarahkan ke `PULSE Dashboard.dc.html`), plus `FRONTEND_SPEC.md` sebagai kontrak data |
| `deploy/demo.py` | Satu perintah untuk seluruh sistem di satu proses, dengan warm-up |
| `deploy/space_boot.py` | Penyala lima proses dalam satu container (untuk jalur Render yang belum dieksekusi) |
| `scripts/smoke.py`, `scripts/error_curve.py`, `scripts/bench.py` | Uji pipeline offline, kurva error vs baseline, benchmark |
| `tests/` | 41 test di 6 file (online learning, drift, loop retrain, replay control, pipeline, engine drift) |
| `.github/workflows/ci.yml`, `retrain.yml` | CI (ruff, pytest, smoke) dan workflow terjadwal harian |
| `docs/` | `METRICS.md`, `bench.json`, `TEST_GAP_MAP.md`, `DEMO.md`, `DEPLOY.md`, `demo.gif`, `error_curve.png` |

## 5. Teknologi yang dipakai dan kenapa

| Teknologi | Kenapa dipilih |
|---|---|
| river 0.21.1 | Library online learning Python. Punya `learn_one` per event, persis yang dibutuhkan |
| SNARIMAX | Model deret waktu yang bisa belajar inkremental dan menerima fitur cuaca. Dipilih karena cocok dengan online learning, bukan karena paling akurat |
| Redis Streams | Message bus paling ringan yang tetap streaming sungguhan. Kafka/Redpanda terlalu berat untuk satu orang |
| fakeredis | Supaya demo bisa jalan di satu proses tanpa server Redis |
| FastAPI + WebSocket | REST async plus dorongan data live ke browser |
| PSI in-house | Diukur lebih tepat dari Evidently di jendela 200 event (lihat bagian 6) |
| Evidently 0.4.25 | Tetap terpasang dan dites, bisa dipilih lewat `DRIFT_ENGINE=evidently` |
| Gemini `gemini-2.5-flash` | Free tier, menulis kartu insiden. Fallback template supaya sistem tidak bergantung pada key |
| Registry JSON lokal | Cepat dikirim. Desainnya cuma perlu ganti satu file untuk pindah ke Supabase |
| Docker Compose | Bentuk deployment yang realistis: lima layanan terpisah plus Redis |
| GitHub Actions | CI lint + test + smoke, dan workflow terjadwal |

Yang sengaja dipangkas untuk v1 (ditulis di README): Redpanda, Supabase, dan DVC. Tenaga difokuskan ke satu hal baru, yaitu streaming plus online learning.

## 6. Angka dan bukti

Semua angka di bawah berasal dari replay `data/sample_aq.csv` (10.080 event sintetis). Bukan data Jakarta sungguhan.

| Angka | Nilai | Sumber |
|---|---|---|
| MAE rata-rata online vs persistence vs seasonal-naive | 3,280 vs 3,417 vs 7,449 µg/m³ | `docs/METRICS.md` |
| Online menang MAE atas persistence | 5 dari 5 stasiun, selisih 2,8% sampai 6,8% | `docs/METRICS.md` |
| RMSE Jaksel, online vs persistence | 6,148 vs 5,931 (online kalah) | `docs/METRICS.md` |
| Event sampai online unggul permanen | 1.102 sampai 2.011 event, Jaksel tidak pernah stabil | `docs/METRICS.md` |
| Throughput pipeline ML | 269,7 event/detik (10.080 event dalam 37,4 detik) | `docs/bench.json` |
| Latensi event ke prediksi | p50 2,6 ms, p95 4,3 ms, p99 16,3 ms | `docs/bench.json` |
| Retrain | 23 kali dalam 335,8 jam data (0,0685 per jam data), 41 per jam dinding di speed 600 | `docs/bench.json` |
| Rate anomali | 58,3% turun ke 0,50% (50 dari 10.080) | `README.md`, `docs/TEST_GAP_MAP.md` bagian 6b, commit `9c3644c` |
| False positive drift di `drift_window=200` | 0 dari 12 (di window 30: 12 dari 12) | `docs/TEST_GAP_MAP.md` temuan 3 |
| PSI in-house vs Evidently PSI | 6/6 vs 5/6 vonis benar | `ml/monitoring/drift.py`, `README.md` |
| MAE saat model diverge (sebelum diperbaiki) | sekitar 5,5e10 µg/m³ | `docs/TEST_GAP_MAP.md`, `ml/online/model.py` |
| Uji stabilitas 30.000 event | MAE 3,27, forecast tertinggi 144 | `ml/online/model.py` |
| Jumlah test | 41 (dari 6 di awal) | `README.md` build log |
| RAM semua proses dalam satu container | sekitar 383 MB dari 512 MB | `docs/DEPLOY.md` bagian 2 |
| Warm-up demo | 600 frame (3.000 event), sekitar 17 detik | `docs/DEMO.md`, `deploy/demo.py` |

Catatan jujur soal `docs/bench.json`: file itu terakhir ditulis 3 Agustus 2026, sebelum detektor anomali diganti tanggal 9 Agustus. Isinya masih mencatat `anomalies_flagged: 5873` (yang 58,3%). Angka 0,50% di README berasal dari pengukuran commit `9c3644c`, bukan dari `bench.json`. Throughput dan latensi di atas juga diukur dengan detektor lama. Perlu dijalankan ulang `python -m scripts.bench` supaya file bukti dan README sinkron.

Catatan RAM: diukur di Windows dengan Python 3.12, sedangkan image Docker-nya Linux Python 3.11. Anggap batas atas kasar.

## 7. Keputusan desain penting

**Tanpa demo publik.** Ini keputusan arsitektur, bukan pekerjaan yang tertinggal. PULSE adalah sistem multi-proses real-time: ingestion, consumer ML, agent, API, dan bus. Hosting gratis hanya memberi satu container kecil yang tidur setelah 15 menit tanpa traffic (Render free, lihat `docs/DEPLOY.md` bagian 7), filesystem-nya hilang tiap restart sehingga riwayat versi model mulai dari nol lagi, dan CPU-nya cuma 0,1. Rekruter yang mengklik link seperti itu akan melihat halaman loading atau dashboard kosong, lalu menyimpulkan sistemnya tidak jalan. Demo gratisan justru salah menggambarkan sistemnya. Trade-off-nya: orang yang tidak meng-clone repo tidak bisa mencoba sendiri. Penambalnya dua: GIF 20 detik di README (direkam dari `python -m deploy.demo` asli, tidak dipentaskan) dan demo lokal satu perintah.

**Demo satu perintah dengan warm-up.** `python -m deploy.demo` menjalankan bus di dalam proses (`REDIS_URL=memory://`), tanpa Docker dan tanpa API key. Sebelum browser terbuka, 600 frame didorong lewat `Engine` asli. Angka 600 diukur: di bawah itu feed insiden kosong dan belum ada retrain akibat drift. Engine yang sudah hangat diserahkan ke thread consumer, jadi model live melanjutkan, bukan mulai dari nol.

**SNARIMAX, bukan deep learning.** Nilai projek ada di siklus hidup di sekitar model. Harganya terlihat di angka: menang tipis, dan kalah RMSE di Jaksel.

**Drift per stasiun, bukan gabungan.** Kalau lima stasiun digabung, satu stasiun yang melonjak dari sekitar 42 ke 125 µg/m³ cuma menghasilkan PSI gabungan 0,19, di bawah ambang 0,2, dan laporannya tidak bisa menyebut stasiun mana (README build log 2 Agustus).

**PSI sebagai default, Evidently opsional.** Default Evidently memakai uji K-S dan mengembalikan p-value sebagai `drift_score`, yang maknanya terbalik (rendah berarti drift). Engine dipilih eksplisit lewat `DRIFT_ENGINE` supaya lokal, CI, dan server memakai detektor yang sama.

**Detektor anomali diganti statistiknya, bukan ambangnya.** Skor HalfSpaceTrees lama menumpuk di desil teratas (median 0,90), sampai spike demo skornya lebih rendah dari event biasa. Harganya: kalibrasi baru ini masuk akal secara konstruksi, belum diuji terhadap label insiden.

**Fallback boleh, asal berisik.** river, Evidently, dan Gemini punya jalur cadangan masing-masing. Pelajaran mahalnya: fallback yang diam kelihatan persis seperti sistem yang jalan, jadi sekarang setiap fallback mencetak log keras dan dites.
