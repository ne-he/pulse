# PULSE: Peta Lubang Test

> Dibuat 1 Agustus 2026. Basis: pembacaan penuh `ml/`, `ingestion/`, `agent/`, `common/`,
> plus `api/` dan `scripts/` sebagai konteks. Test yang ada saat pemetaan: **satu file**,
> `tests/test_pipeline.py`, berisi 6 test.

Dokumen ini bukan daftar keinginan. Isinya: apa yang sudah ketutup, apa yang belum,
dan seberapa mahal kalau yang belum itu diam-diam rusak. Urutannya risiko, bukan abjad.

---

## 1. Ringkasan cakupan sebelum upgrade

| Modul | Fungsi publik | Ketutup test | Catatan |
|---|---|---|---|
| `common/health.py` | `pm25_to_aqi`, `aqi_category`, `category_for_pm25` | 2 dari 3 | `category_for_pm25` tidak pernah dipanggil test |
| `common/config.py` | `Settings`, `Streams` | tidak langsung | dipakai implisit di semua test |
| `common/schemas.py` | 5 model pydantic + `STATIONS` | tidak langsung | validasi skema tidak pernah diuji sendiri |
| `common/redis_bus.py` | `publish`, `StreamReader`, `tail`, `recent` | **0** | butuh Redis, tidak ada fake |
| `ingestion/gen_sample.py` | `generate`, `main` | **0** | dipakai tidak langsung oleh baseline |
| `ingestion/replay.py` | `run`, `_event_from_row`, `_inject_spike` | **0** | butuh Redis |
| `ingestion/producer.py` | `run` (live polling) | **0** | butuh jaringan |
| `ml/online/model.py` | `StationModel.*`, `exog_features` | sebagian | dites "n bertambah" dan "band mengurung", **bukan** "state model berubah" |
| `ml/online/anomaly.py` | `AnomalyDetector.score` | ya | spike 10x tertangkap |
| `ml/online/consumer.py` | `Engine.process`, `_track_drift`, `_retrain` | **0** | inilah jalur kritisnya, dan nol test |
| `ml/monitoring/drift.py` | `check_drift`, `_psi`, `_drift_psi`, `_drift_evidently` | sebagian | dites di level DataFrame utuh, tidak per stasiun |
| `ml/registry/registry.py` | `register`, `save_card`, `active`, `list_versions` | ya | roundtrip dasar |
| `ml/modelcard/generate.py` | `generate_card` | dangkal | hanya dicek string `"Model Card"` ada |
| `ml/batch/baseline.py` | `evaluate`, `main` | **0** | pembanding P6, tidak pernah dijalankan test |
| `agent/incident.py` | `build_incident`, template | tidak di pytest | hanya lewat `scripts/smoke.py` |
| `api/main.py` | 12 route REST + WS | **0** | tidak ada TestClient sama sekali |

---

## 2. Lubang diurutkan berdasarkan risiko

Skala risiko = (seberapa keras klaim README-nya) x (seberapa sunyi kegagalannya).
Kegagalan sunyi itu yang paling mahal: kode tetap jalan, angkanya tetap keluar, cuma bohong.

### RISIKO 1 (tertinggi): `learn_one` benar-benar mengubah state model

**Klaim README:** "learn_one() dipanggil di setiap event (true online learning, bukan batch
retraining yang disamarkan)". Ini kalimat pembeda utama projek.

**Kenyataan kode:** `StationModel.observe()` di `ml/online/model.py:99` memanggil
`self.model.learn_one(y, x=exog)` di dalam `try/except`. Kalau melempar exception sekali saja,
`self.model` di-set `None` dan `self.kind` jadi `"baseline"` **secara permanen**. Setelah itu
`learn_one` tidak pernah dipanggil lagi, forecast jatuh ke last-value, dan **tidak ada apa pun
yang gagal keras**. Test yang ada (`test_station_model_learns_and_bands_bracket_point`) hanya
memeriksa `model.n == 60` dan `model.mae is not None`. Dua-duanya tetap lulus dalam mode
baseline. Jadi klaim inti projek ini bisa palsu total tanpa satu test pun merah.

**Yang hilang:** snapshot state internal river sebelum dan sesudah N event, lalu bandingkan.
Plus assert `model.kind == "river"` supaya degradasi diam-diam ketahuan.

### RISIKO 2: drift terdeteksi saat distribusi digeser sengaja

**Kenyataan kode:** ada `test_drift_detects_shift`, tapi ia menggeser **keempat** fitur
sekaligus (pm25, temp, humidity, wind_speed) dengan jarak sangat lebar. Itu ujian yang terlalu
gampang. Yang tidak diuji: geseran **hanya pada PM2.5**, yang justru kasus nyata (kebakaran,
kemacetan, musim kemarau) sementara cuaca relatif tetap. Dengan `drift_threshold = 0.5` dan
4 fitur, satu fitur drift menghasilkan share 0.25, jadi `dataset_drift` **False**.

Selain itu jalur Evidently (`_drift_evidently`) tidak pernah dites langsung. Fallback PSI
menelan semua exception, jadi kalau Evidently rusak, tidak ada yang tahu selain baris print.

### RISIKO 3: drift memicu retrain dan menghasilkan versi model baru

**Kenyataan kode:** `Engine._track_drift` dan `Engine._retrain` di `ml/online/consumer.py`
adalah satu-satunya tempat loop tertutup itu benar-benar terjadi, dan keduanya punya **nol
test**. Test yang ada berhenti di `check_drift` (level DataFrame) dan `Registry.register`
(level penyimpanan), lalu berasumsi jembatannya ada. Jembatan itu sendiri tidak pernah diseberangi.

Rinciannya, yang tidak terverifikasi:
- gating window: butuh `drift_window` event untuk mengisi reference, lalu `drift_window` lagi
  sebelum cek pertama (`consumer.py:113-124`). Off-by-one di sini berarti drift tidak pernah dicek.
- `Engine.process` bergantung pada Redis (`publish`) di 3 tempat, sehingga tidak bisa dites
  tanpa fake client. Belum ada fake client di repo.
- reset reference setelah retrain (`consumer.py:160`).

### RISIKO 4: model card ter-generate dengan isi yang benar

**Kenyataan kode:** `test_registry_and_model_card_roundtrip` hanya assert substring
`"Model Card"` ada di file. Tidak dicek: nomor versi benar, MAE/RMSE benar-benar muncul
sebagai angka, alasan retrain (`reason`) ikut tertulis, tabel drift terisi kalau report
dilewatkan. Model card adalah artefak governance projek ini. Kalau MAE-nya kosong atau
alasannya salah, dokumen itu jadi hiasan.

### RISIKO 5: drift dihitung mencampur semua stasiun

**Kenyataan kode:** `ml/monitoring/drift.py` **tidak menyebut `station_id` sama sekali**
(diverifikasi ulang 1 Agustus, temuan 12 Juli masih persis berlaku). `Engine.reference_rows`
dan `current_rows` adalah satu list global lintas stasiun. Lima stasiun Jakarta punya baseline
PM2.5 yang berbeda secara struktural (`gen_sample.py:44`: `station_base = 18 + 6 * index`,
jadi 18 sampai 42 mikrogram per meter kubik). Dua akibat:

1. **False drift.** Kalau urutan kedatangan event berubah, komposisi stasiun di window ikut
   berubah, dan PSI melihat pergeseran distribusi yang sebenarnya cuma pergeseran sampling.
2. **Drift lokal tertelan.** Satu stasiun melonjak keras tapi empat lainnya tenang, distribusi
   gabungan hampir tidak bergerak. Justru kasus ini yang paling penting untuk ditangkap.

Dikerjakan di Langkah 2 upgrade ini.

### RISIKO 6: baseline batch tidak pernah dibandingkan dengan model online

`ml/batch/baseline.py` mencetak angka ke stdout dan tidak dipanggil siapa pun. README belum
punya section Results. Klaim "online learning" tidak punya pembanding, jadi tidak ada yang
bisa menilai apakah belajar terus itu ada gunanya. Dikerjakan di Langkah 3.

### RISIKO 7: lapisan transport (Redis) tidak punya test sama sekali

`common/redis_bus.py`, `ingestion/replay.py`, `api/main.py` semuanya nol test karena butuh
Redis hidup. Risikonya nyata tapi lebih rendah dari 1 sampai 4: kegagalan di sini **berisik**,
bukan sunyi. Kalau Redis tidak nyambung, semuanya mati dan langsung kelihatan. Solusi yang
tepat adalah fake in-memory client, dan itu memang dibuat di Langkah 1 untuk keperluan
`Engine`, jadi pintunya terbuka untuk nanti.

### RISIKO 8: skema pydantic dan route API

`Prediction` punya `trend: Literal["up","down","flat"]` dan `Alert.type: Literal[...]`.
Kalau ada yang menulis nilai di luar itu, pydantic melempar saat runtime di dalam
`Engine.process`, yang tertelan `except Exception` di `consumer.py:174`. Berisik di log,
sunyi di sistem. Prioritas rendah untuk sekarang.

---

## 3. Hasil: apa yang ketahuan setelah test-nya benar-benar ditulis

Peta di atas ditulis dari pembacaan kode. Setelah test-nya dijalankan, dua dugaan naik
kelas dari "risiko" jadi "bug yang sudah jalan di produksi".

### Temuan 1: online learning tidak pernah jalan sama sekali (RISIKO 1, terkonfirmasi)

`ml/online/model.py` membangun `SNARIMAX(p=2, d=0, q=1, m=0)`. Di river, `m` itu periode
musiman dan nilai "tanpa musiman" adalah **1**, bukan 0. Dengan `m=0`, SNARIMAX menyusun
lag feature lewat `range(self.m - 1, self.m * self.sp, self.m)`, dan step 0 melempar
`ValueError: range() arg 3 must not be zero` pada panggilan `learn_one` yang **pertama**.

Blok `except` di `observe()` lalu menyetel `self.model = None` dan `self.kind = "baseline"`
secara permanen, tanpa satu baris log pun. Jadi setiap "forecast online" di repo ini
sebenarnya persistence (nilai terakhir). Klaim utama README salah, dan test lama tidak
bisa melihatnya karena dua assert-nya (`model.n == 60`, `model.mae is not None`) tetap
benar dalam mode terdegradasi.

Ini pelajaran yang lebih besar dari bug-nya: **fallback yang diam terlihat persis seperti
sistem yang bekerja.** Sekarang kedua jalur fallback nge-log keras, dan
`test_baseline_fallback_is_visible_not_silent` mengunci `kind` sebagai sinyal.

### Temuan 2: begitu learning dinyalakan, modelnya diverge

Setelah `m=1`, model baru benar-benar belajar, dan langsung meledak. Term MA (`q=1`)
mengumpankan residual model sendiri sebagai fitur, jadi di learning rate default river
error-nya saling menguatkan. Replay 10.080 event sampel: MAE 1-step rata-rata **5,5e10**
mikrogram per meter kubik, forecast tertinggi 1,8e11.

Empat konfigurasi diukur (bukan ditebak), baseline persistence = 3,42:

| Konfigurasi | MAE | Hasil |
|---|---|---|
| `p=2 d=0 q=1`, default river | 5,5e10 | diverge |
| `p=3 d=1 q=1`, SGD(0.005) | 5,0e10 | diverge |
| `p=2 d=1 q=0`, default river | 5,71 | stabil, kalah dari persistence |
| `p=2 d=1 q=1`, SGD(0.002) ilr=0.005 | **3,28** | stabil, menang tipis |

Yang dipakai baris terakhir, lalu diuji ulang di 30.000 event: MAE 3,27, forecast tertinggi
144. Ditambah guard kewajaran di `_point_forecast` supaya angka diverge tidak akan pernah
sampai ke dashboard.

### Temuan 3: PSI sangat rapuh di window kecil

Ditemukan saat mencoba mempercepat test dengan mengecilkan `drift_window`. PSI membagi
referensi jadi 10 bin kuantil, jadi window kecil menyisakan sedikit sampel per bin dan
PSI menyala karena noise. Diukur di data ini, dua window dari regime yang sama:

| `drift_window` | Sampel per bin | False positive |
|---|---|---|
| 30 | 3 | 12 dari 12 |
| 60 | 6 | 12 dari 12 |
| 100 | 10 | 9 dari 12 |
| 150 | 15 | 1 dari 12 |
| 200 (default produksi) | 20 | **0 dari 12** |

Default produksi aman, jadi tidak ada yang diubah. Tapi ini artinya `drift_window` bukan
knob bebas: menurunkannya untuk "biar responsif" akan membuat sistem retrain terus-menerus
karena angin. Test sengaja jalan di window produksi, alasannya ditulis di
`tests/conftest.py::isolated_registry`.

## 4. Yang dikerjakan di upgrade ini

| Risiko | Test baru | File |
|---|---|---|
| 1 | `test_online_learning_changes_state` | `tests/test_online_learning.py` |
| 2 | `test_drift_detected_on_shifted_distribution` | `tests/test_drift_retrain_loop.py` |
| 3 | `test_drift_triggers_retrain_and_new_version` | `tests/test_drift_retrain_loop.py` |
| 4 | `test_model_card_generated_correctly` | `tests/test_drift_retrain_loop.py` |
| 5 | drift per `station_id` + `test_drift_in_one_station_triggers_retrain` | `ml/monitoring/drift.py`, `ml/online/consumer.py`, `tests/test_drift_per_station.py` |
| 6 | `scripts/error_curve.py` + section Results | `scripts/error_curve.py`, `docs/METRICS.md` |

Sisanya (risiko 7 dan 8) dicatat, tidak dikerjakan.

Hasil akhir: dari 6 test di 1 file jadi **27 test di 4 file, semuanya lulus**. Satu test
sempat merah (kegagalannya sudah ada sebelum upgrade ini), lalu dibereskan dengan memecahnya
jadi dua test setelah akar masalahnya diukur. Ceritanya di bagian 6a.

## 5. Utang test yang sengaja dibiarkan

- `api/main.py`: 12 route REST plus WebSocket, nol test. Butuh `fastapi.testclient` plus fake
  Redis async. Sekitar 2 jam kerja.
- `ingestion/producer.py`: jalur live polling OpenAQ dan Open-Meteo, nol test. Butuh mock httpx.
- `ml/monitoring/drift.py` jalur Evidently: hanya diuji tidak langsung. Kalau Evidently ganti
  API, fallback PSI menyelamatkan tanpa ada yang tahu. Idealnya ada satu test yang memaksa
  jalur Evidently dan gagal keras kalau ia tidak dipakai.
- `common/schemas.py`: validasi `Literal` tidak pernah diuji.

## 6. Masalah yang dilaporkan, TIDAK dikerjakan

Di luar cakupan upgrade ini. Dicatat supaya tidak hilang.

### a. `test_anomaly_detector_flags_spike` merah, SUDAH DIBERESKAN

*(Bagian ini awalnya masuk daftar "tidak dikerjakan". Setelah diinstrumentasi, akar
masalahnya ternyata bukan yang diduga di sini, jadi ikut dikerjakan. Ditinggal apa adanya
supaya jejak diagnosisnya kelihatan.)*

Kegagalannya memang sudah ada sebelum upgrade ini. Diverifikasi dengan mengekstrak HEAD
bersih lewat `git archive` lalu menjalankan suite-nya di sana: gagal juga.

**Dugaan awal (SALAH):** beda perilaku antar versi scikit-learn. Ini keliru. `MinMaxScaler`
yang dipakai di sini milik river (`river.preprocessing`), bukan sklearn, jadi versi sklearn
tidak ada hubungannya.

**Sebab sebenarnya, diukur langsung:** `score_one` dipanggil SEBELUM `learn_one` di
`AnomalyDetector.score`. river `MinMaxScaler` mengembalikan 0 selama min masih sama dengan
max. Di warmup yang datar sempurna (100 event bernilai 20.0 persis), scaler baru melihat
satu nilai berbeda, jadi spike 200.0 di-scale jadi 0 dan tidak terlihat oleh HalfSpaceTrees
**pada saat event itu tiba**. Scaler baru belajar rentang barunya sesudah itu, jadi alarmnya
mendarat di event berikutnya.

Angka terukur pada input konstan: event 100 skor 0,0000, event 102 skor 0,9155.

Yang menentukan: pada input dengan noise kecil (deviasi 2, yang jauh lebih mirip feed
sensor sungguhan) spike-nya **tertangkap tepat di event 100 dengan skor 0,9683**. Jadi ini
bukan cacat detektor, ini artefak input sintetis yang datar sempurna.

**Yang dilakukan:** assert-nya tidak dilonggarkan. Test lama dipecah jadi dua yang keduanya
menguji hal nyata:
- `test_anomaly_detector_flags_spike_on_realistic_signal` menuntut flag tepat di event
  spike, pada sinyal bernoise. Ini kondisi yang sebenarnya dihadapi di produksi.
- `test_anomaly_detector_flags_spike_on_constant_signal_but_late` mengunci perilaku
  terlambat itu pada sinyal datar, dengan batas latensi 100 sampai 105 event. Kalau ada
  yang menukar urutan score/learn atau mengganti scaler-nya, perubahan latensi deteksi
  muncul sebagai test merah, bukan regresi senyap.

> **Catatan 2026-08-09:** keterlambatan dua event itu sudah hilang bersama pergantian
> detektor di bagian 6b. Penyebabnya memang MinMaxScaler yang mengembalikan 0 selama
> min == max, dan detektor residual tidak punya titik buta itu. Tes keduanya diganti nama
> jadi `test_anomaly_detector_flags_spike_on_constant_signal_without_lag` dan sekarang
> mengunci deteksi tepat di event 100.

### b. Detektor anomali menyala di 58 persen event — SUDAH DIPERBAIKI (2026-08-09)

`python -m scripts.bench` mencatat **5.873 anomali dari 10.080 event**. Kalau lebih dari
separuh aliran data disebut anomali, kata itu kehilangan arti, dan feed insiden di
dashboard jadi spam.

Dugaan lama, bahwa `anomaly_threshold = 0.85` terlalu longgar, ternyata salah alamat.
Masalahnya bukan di angka threshold-nya, tapi di **statistik yang dipakai buat memutuskan**.
Skor HalfSpaceTrees di feed ini punya rata-rata 0.82 dan median 0.90, jadi hampir semua
event tertumpuk di desil teratas dan tidak ada titik potong yang bisa menyeleksi apa pun.
Buktinya: spike demo 120 µg/m³ dapat skor 0.9832, sementara event biasa mencapai 0.9964,
jadi spike yang sengaja disuntik pun tidak masuk persentil teratas. Menyetel ulang scaling
fitur malah memperparah kejenuhan (rata-rata naik ke 0.96), dan gerbang rolling-quantile
juga tidak menolong karena distribusinya memang rapat di ujung atas.

Jadi yang diganti statistiknya, bukan knob-nya. Anomali sekarang = **kejutan forecast satu
langkah**: residual persistence dibagi estimasi robust (MAD) dari ukuran langkah normal
stasiun itu, lalu dipadatkan jadi `score = z / (1 + z)` supaya threshold [0, 1] yang lama
tetap berlaku dan sekarang bisa dibaca langsung dalam sigma (`0.85` artinya `z >= 5.67`).
Ditambah lantai materialitas: anomali di udara kategori Good tidak dilaporkan, karena
bacaan 2 µg/m³ yang aneh secara statistik tetap bukan insiden yang perlu ditindak.

Hasil di sampel 10.080 event yang sama: **0,50 persen event ditandai** (dari 58,3 persen),
nol alarm palsu di 500 event stasiun bersih, dan lonjakan 10x tertangkap di event kejadian
itu sendiri (sebelumnya telat dua event).

Yang **belum** selesai: masih belum ada labelled incident set buat feed ini. Jadi klaim
jujurnya adalah "skornya sekarang punya arti dalam sigma dan rate-nya masuk akal", bukan
"detektornya sudah benar". Kalibrasi berlabel tetap pekerjaan terbuka.

Celah tesnya juga ditutup: suite lama cuma menguji sensitivitas (apakah spike tertangkap),
tidak pernah menguji spesifisitas. Itu sebabnya detektor yang menyala di 58 persen event
bisa duduk di repo dengan suite hijau. Sekarang ada
`test_anomaly_detector_is_quiet_on_a_calm_feed`,
`test_anomaly_detector_rate_stays_selective_across_regimes`, dan
`test_anomaly_detector_ignores_odd_but_clean_air`.

### b2. Kontrol demo tidak pernah sampai ke replay engine — SUDAH DIPERBAIKI (2026-08-09)

`ingestion/replay.py` menguras control stream pakai `XREAD ... $` **tanpa BLOCK**. `$`
berarti "id yang lebih besar dari maksimum stream saat pemanggilan", dan karena tidak
blocking, tidak ada jeda buat id baru muncul: panggilan itu selalu balik kosong dan
offset-nya tidak pernah maju. Akibatnya semua perintah `play`, `pause`, `set_speed`,
`seek`, dan `trigger_spike` dibuang diam-diam.

Ini bukan cacat kecil: tombol `[SPIKE]` adalah satu-satunya kontrol yang dipakai di skrip
demo README, dan tombol itu tidak pernah berfungsi. Tidak ada yang menangkapnya karena
tidak ada satu pun tes yang menyentuh jalur kontrol, dan kalau dicoba manual gejalanya
cuma "replay-nya jalan terus", yang persis sama dengan tampilan sistem yang sehat.

Diperbaiki dengan menyemai offset dari id terakhir stream (`control_start_offset`), yang
tetap mempertahankan maksud aslinya, yaitu mengabaikan perintah yang dikirim sebelum
worker ini hidup. Dikunci oleh `tests/test_replay_control.py`.

### c. `gen_sample.py` tidak sepenuhnya reproducible

`generate()` memakai `pd.Timestamp.utcnow()` sebagai jangkar rentang tanggal, jadi
walaupun rng-nya di-seed 42, jam-nya bergeser tiap hari dan pola diurnalnya ikut geser.
Regenerasi di hari berbeda menghasilkan angka yang sedikit berbeda. Angka di
`docs/METRICS.md` mengacu ke file sampel yang dibuat 2026-08-02, dan ini ditulis eksplisit
di sana. Perbaikan yang wajar: jangkar ke tanggal tetap lewat env var. **Tidak diubah.**

### d. `scikit-learn` tidak di-pin di `requirements.txt`

river menariknya sebagai dependency transitif, jadi versinya bebas mengambang. Itu
kemungkinan penyebab poin (a). **Tidak diubah.**
