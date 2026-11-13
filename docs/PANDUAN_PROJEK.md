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

## 8. Update terbaru

| Tanggal | Yang terjadi |
|---|---|
| 22 Jun 2026 | Kerangka penuh dibangun, semua layanan tersambung di mode replay (README build log) |
| 15 Jul 2026 | Commit pertama `92cc1cd`: backend end-to-end plus dashboard tersambung |
| 2 sampai 3 Agu 2026 | Test ditulis untuk membuktikan klaim, dan dua klaim ternyata salah. Online learning tidak pernah jalan karena `m=0` di SNARIMAX bikin `learn_one` pertama error lalu diam-diam jatuh ke baseline. Setelah dinyalakan, model diverge, diperbaiki dengan `d=1` dan learning rate lebih pelan. Drift dibuat per stasiun. Kurva error dan benchmark ditambahkan (commit `14a419a`) |
| 5 Agu 2026 | Jalur deploy satu container plus dashboard Vercel disiapkan, lalu target dipindah dari HF Space ke Render karena Docker Space butuh langganan PRO sejak sekitar Juli 2026 (`78b03b8`) |
| 9 Agu 2026 | Detektor anomali diganti: 58,3% jadi 0,50% (`9c3644c`). Tombol `[SPIKE]` yang ternyata tidak pernah berfungsi diperbaiki (`9445b74`). `python -m deploy.demo` lahir (`78aabd1`) |
| 14 Agu 2026 | CI merah 6 run karena detektor drift beda antara lokal (PSI) dan CI (Evidently), diperbaiki dengan engine eksplisit (`6904b25`). Model Gemini lama pensiun dan kartu insiden diam-diam jadi template, diganti ke `gemini-2.5-flash` (`ca0f44f`) |
| 26 Sep 2026 | README menyatakan terang: tanpa demo publik, by design (`5dcab67`) |
| 28 Sep 2026 | Em dash dibersihkan dari docs, komentar, dan teks UI |
| 11 Nov 2026 | GIF demo 20 detik (`docs/demo.gif`) dipasang di bawah pitch README, direkam dari `python -m deploy.demo` asli: chart forecast PM2.5 dengan pita ketidakpastian, sparkline per stasiun, model health, drift monitor, feed insiden. Kartu insiden di GIF berlabel `[template]` karena direkam tanpa key Gemini (`84b004a`) |

## 9. Batasan jujur dan yang belum dikerjakan

- Data demo sintetis. Mode live (OpenAQ plus Open-Meteo) ada di kode tapi `ingestion/producer.py` belum punya test.
- Keunggulan online learning tipis (2,8% sampai 6,8% MAE), butuh 1.100 sampai 2.000 event sebelum unggul, dan kalah RMSE di Jaksel.
- Detektor anomali belum dikalibrasi terhadap label insiden. Klaim yang sah cuma "skornya punya arti dalam sigma dan rate-nya masuk akal".
- Drift yang hanya terjadi di PM2.5 sementara cuaca tetap tidak memicu retrain, karena 1 dari 4 fitur = share 0,25, di bawah 0,5. Perilaku ini dikunci test `test_pm25_only_shift_flags_the_feature_but_not_the_dataset`.
- `api/main.py` (12 route plus WebSocket) belum punya test. Begitu juga validasi `Literal` di skema (`docs/TEST_GAP_MAP.md` bagian 5).
- Workflow `retrain.yml` yang jalan harian sebenarnya baru meregenerasi sampel dan mengevaluasi baseline batch. Belum ada promosi versi ke registry remote (masih TODO Supabase di file itu).
- Registry masih JSON lokal, DVC belum dipasang.
- `docs/bench.json` belum diperbarui sejak detektor anomali diganti (lihat bagian 6).
- `gen_sample.py` menjangkar tanggal ke hari ini, jadi regenerasi di hari lain menggeser angka sedikit. `scikit-learn` tidak di-pin.
- Dockerfile dan jalur Render belum pernah di-build, karena Docker tidak terpasang di mesin pengembangan (`docs/DEPLOY.md`).
- Beberapa dokumen basi: `PROJECT_BRIEF.md` masih menyebut HalfSpaceTrees dan "deploy publik adalah langkah berikutnya", `docs/DEPLOY.md` bagian 5 masih membahas masalah 58,3% dan menyebut 27 test, bagian Milestones README masih menulis target deploy publik.

## 10. Cara menjalankan dan deploy

Jalur demo (yang dipakai untuk presentasi):

```
pip install -r requirements.txt
python -m deploy.demo
```

Browser terbuka di http://localhost:8000 setelah warm-up sekitar 17 detik. Di Windows bisa klik dua kali `demo.cmd`. Flag yang berguna: `--warm 0` (cold start sengaja), `--no-browser` (untuk merekam), `--speed 1200`, `--fresh` (hapus registry, versi mulai dari v1), `--port 9000`. Satu hal yang perlu dicek sendiri: dashboard membaca backend dari `?api=` dengan default `http://localhost:8000`, jadi kalau pakai `--port` lain kemungkinan perlu membuka dashboard dengan `?api=http://localhost:9000`. Ini kesimpulan dari membaca kode, belum dicoba `[?]`.

Jalur bentuk deployment sungguhan (lima layanan terpisah):

```
cp .env.example .env
docker compose up --build
```

Dashboard di http://localhost:3000, API docs di http://localhost:8000/docs.

Jalur cek tanpa Docker:

```
python -m scripts.smoke
python -m scripts.error_curve
python -m scripts.bench
pytest -q
```

Soal deploy publik: rencana awal ke Hugging Face Docker Space batal karena Docker Space butuh PRO sejak Juli 2026. `docs/DEPLOY.md` membahas alternatif Render free (Web Service Docker, 512 MB RAM, 0,1 CPU) untuk backend plus Vercel statis untuk dashboard, lengkap dengan anggaran RAM dan tabel troubleshooting. Rencana itu belum dieksekusi dan tidak ada URL Render atau Vercel milik PULSE. Variabel env yang relevan (nama saja): `GEMINI_API_KEY`, `ANOMALY_THRESHOLD`, `REPLAY_SPEED`, `INGEST_MODE`, `OPENAQ_API_KEY`, `DRIFT_ENGINE`, `REDIS_URL`.

## 11. Cara menjelaskan ke rekruter

**Pitch 30 detik (Indonesia)**

"Kebanyakan projek ML berhenti di 'saya melatih model'. PULSE mulai dari situ. Ini sistem streaming kualitas udara untuk lima stasiun Jakarta. Modelnya belajar dari setiap bacaan baru lewat river, menandai lonjakan berdasarkan seberapa jauh bacaan menyimpang dari normalnya dalam sigma, dan memantau drift per stasiun. Kalau pola bergeser, sistem membuat versi model baru, menulis ulang model card, dan agent Gemini menarasikan kejadiannya. Saya menulis test untuk klaim saya sendiri, dan test itu menemukan bahwa online learning-nya sempat tidak pernah jalan. Semua angka, termasuk stasiun tempat model saya kalah, ada di repo."

**Pitch 30 detik (English)**

"Most ML portfolios stop at 'I trained a model'. PULSE starts there. It is a streaming air-quality system for five Jakarta stations. The model learns from every single reading with river, flags spikes by how many sigmas they sit from that station's normal, and monitors drift per station. When the data shifts, the system mints a new model version, rewrites its model card, and a Gemini agent narrates what happened. I wrote tests for my own claims, and they caught that online learning had silently never run. Every number is in the repo, including the station where my model loses."

**Pertanyaan yang mungkin muncul**

1. **"Kok gak ada link live?"** Sengaja. PULSE itu lima proses yang jalan terus. Hosting gratis memberi satu container yang tidur setelah 15 menit, CPU 0,1, dan disk yang hilang tiap restart, jadi yang dilihat orang cuma layar loading atau dashboard kosong. Gantinya ada GIF 20 detik dari run asli di README, dan `python -m deploy.demo` yang jalan dengan satu perintah. Kalau mau, saya bisa share screen dan menekan tombol spike langsung.
2. **"Apa bedanya online learning dengan retrain berkala?"** Retrain berkala melatih ulang dengan batch data tiap jadwal. Di PULSE, `learn_one` dipanggil di setiap event. "Retrain" saat drift cuma membekukan state itu jadi versi resmi plus dokumentasi.
3. **"Seberapa bagus modelnya?"** MAE rata-rata 3,28 vs persistence 3,42, menang di 5 stasiun tapi cuma 2,8% sampai 6,8%, dan di Jaksel kalah RMSE. Persistence itu baseline kuat di data 10 menit, dan saya memilih melaporkannya apa adanya.
4. **"Bagaimana kamu tahu sistemnya benar-benar jalan?"** Dari test yang menguji state model berubah, bukan cuma "outputnya ada". Test itu yang menemukan bug `m=0`: setiap forecast sempat persistence diam-diam. Pelajarannya, fallback yang diam kelihatan seperti sistem sehat.
5. **"Kenapa PSI buatan sendiri, bukan Evidently?"** Diukur. Di jendela 200 event, PSI berbasis kuantil benar 6 dari 6 kasus, Evidently 5 dari 6 dan sempat menandai stasiun yang tidak bergerak. Evidently tetap terpasang dan dites, bisa dipilih lewat env.
6. **"Anomali dideteksi bagaimana?"** Selisih bacaan dengan bacaan sebelumnya, dibagi sigma dari MAD residual terbaru stasiun itu. Detektor lama (HalfSpaceTrees) menandai 58,3% event, yang baru 0,50%. Belum ada data label, jadi saya tidak mengklaim detektornya "akurat".
7. **"Apa yang terjadi kalau Gemini mati?"** Kartu template deterministik selalu siap, dan label `generated_by` menunjukkan sumbernya. Setelah 3 kegagalan beruntun agent berhenti mencoba supaya demo tidak lag. Pernah kejadian model Gemini lama pensiun dan semua kartu diam-diam jadi template, sekarang sudah pakai `gemini-2.5-flash`.
8. **"Kalau dibawa ke produksi beneran, apa yang kurang?"** Registry pindah ke database hosted, test untuk API dan ingestion live, kalibrasi anomali dengan data berlabel, dan layanan dipisah lagi seperti di `docker-compose.yml`, bukan satu container.

## 12. Kamus istilah

| Istilah | Artinya dalam bahasa sederhana |
|---|---|
| PM2.5 | Partikel debu sangat halus (di bawah 2,5 mikrometer) yang bisa masuk ke paru-paru. Satuannya µg/m³ |
| AQI | Indeks kualitas udara skala US EPA, dari Good sampai Hazardous |
| Streaming | Data diproses satu per satu saat datang, bukan dikumpulkan dulu |
| Online learning | Model belajar dari setiap data baru, langsung, tanpa melatih ulang dari nol |
| `learn_one` | Fungsi river untuk belajar dari satu contoh data |
| river | Library Python untuk machine learning yang belajar per event |
| SNARIMAX | Model deret waktu yang memakai nilai-nilai sebelumnya plus fitur luar seperti cuaca |
| Persistence baseline | Tebakan paling malas: nilai berikutnya sama dengan nilai terakhir |
| Seasonal-naive | Tebakan: nilai sekarang sama dengan nilai jam yang sama kemarin |
| MAE | Rata-rata besar kesalahan tebakan |
| RMSE | Mirip MAE, tapi kesalahan besar dihukum lebih berat |
| Pita ketidakpastian | Rentang di sekitar tebakan yang menunjukkan seberapa yakin model |
| Anomali | Bacaan yang jauh menyimpang dari pola normal stasiun itu |
| MAD | Median dari jarak tiap nilai ke median. Ukuran sebaran yang tidak gampang terpengaruh nilai ekstrem |
| Sigma / z-score | Seberapa jauh sebuah nilai dari normal, diukur dalam satuan sebaran |
| Drift | Pola data yang masuk bergeser dari pola yang dulu dipelajari model |
| PSI | Population Stability Index, angka yang mengukur seberapa beda dua distribusi. Di atas 0,2 dianggap bergeser |
| Model registry | Buku catatan versi model: v1, v2, dan seterusnya, dengan metrik dan alasannya |
| Model card | Dokumen satu halaman yang menjelaskan model: kegunaan, data, metrik, batasan |
| Redis Streams | Papan pengumuman berurutan tempat layanan saling kirim pesan |
| fakeredis | Tiruan Redis yang hidup di dalam proses Python, dipakai untuk demo |
| WebSocket | Saluran dua arah yang tetap terbuka, sehingga server bisa mendorong data ke browser |
| Replay | Memutar ulang data lama seolah-olah sedang terjadi sekarang |
| Fallback | Jalur cadangan kalau komponen utama gagal |
| CI | Continuous Integration: test otomatis tiap kali kode di-push |
