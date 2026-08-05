"""Nyalakan seluruh loop PULSE di dalam SATU container (Hugging Face Spaces).

`docker-compose.yml` memecah sistem jadi lima layanan: redis, ingestion, ml,
agent, api. Docker Space cuma memberi satu container dan satu port, jadi
kelimanya harus hidup di bawah satu proses induk. File ini yang mengurusnya.

Urutannya penting. Redis dinyalakan lebih dulu dan BARU dianggap siap setelah
benar-benar menjawab PING, bukan setelah prosesnya muncul. Tiga worker yang
menyusul semuanya membuka koneksi ke bus pada baris pertama, jadi kalau mereka
start duluan mereka mati seketika dan Space-nya kelihatan hidup padahal isinya
kosong: API menyala, dashboard connect, tapi tidak ada satu pun event mengalir.

Proses utama diserahkan ke uvicorn karena itu yang harus memegang port. Worker
jalan sebagai anak proses dan ikut mati waktu induknya berhenti.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

REDIS_PORT = os.environ.get("REDIS_PORT", "6379")
PORT = os.environ.get("PORT", "7860")

# Tiga worker yang membentuk loop: sumber data, model online, narator insiden.
WORKERS: list[tuple[str, list[str]]] = [
    ("ingestion", [sys.executable, "-m", "ingestion.run"]),
    ("ml", [sys.executable, "-m", "ml.online.consumer"]),
    ("agent", [sys.executable, "-m", "agent.incident"]),
]

children: list[tuple[str, subprocess.Popen]] = []


def log(msg: str) -> None:
    print(f"[boot] {msg}", flush=True)


def start_redis() -> subprocess.Popen:
    """Redis in-memory saja. Space filesystem ephemeral, persistensi tidak ada gunanya
    dan RDB/AOF cuma bikin fork lambat tiap snapshot."""
    proc = subprocess.Popen(
        [
            "redis-server",
            "--port", REDIS_PORT,
            "--save", "",
            "--appendonly", "no",
            "--daemonize", "no",
            "--loglevel", "warning",
        ]
    )
    children.append(("redis", proc))
    return proc


def wait_for_redis(timeout_sec: float = 30.0) -> None:
    """Tunggu sampai Redis menjawab PING. Proses yang sudah spawn belum tentu sudah
    menerima koneksi, dan selisih beberapa ratus milidetik itu cukup untuk
    membunuh ketiga worker sekaligus."""
    import redis  # diimpor di sini supaya pesan errornya jelas kalau deps belum lengkap

    client = redis.Redis(host="127.0.0.1", port=int(REDIS_PORT), decode_responses=True)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            if client.ping():
                log(f"redis siap di port {REDIS_PORT}")
                return
        except Exception:  # noqa: BLE001, koneksi ditolak selama server belum listen
            time.sleep(0.25)
    raise RuntimeError(f"redis tidak menjawab PING dalam {timeout_sec:.0f} detik")


def start_workers() -> None:
    for name, cmd in WORKERS:
        log(f"start worker {name}")
        children.append((name, subprocess.Popen(cmd)))


def report_dead_workers() -> None:
    """Worker yang mati tidak menjatuhkan API. Tanpa baris log ini, Space-nya
    kelihatan sehat padahal loopnya sudah putus, jadi kematiannya dibikin berisik."""
    for name, proc in children:
        code = proc.poll()
        if code is not None:
            log(f"PERINGATAN: {name} berhenti lebih awal (exit {code})")


def shutdown(*_args: object) -> None:
    for name, proc in reversed(children):
        if proc.poll() is None:
            log(f"stop {name}")
            proc.terminate()
    for _name, proc in reversed(children):
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    signal.signal(signal.SIGTERM, lambda *_: (shutdown(), sys.exit(0)))

    try:
        start_redis()
        wait_for_redis()
        start_workers()
        time.sleep(3)  # beri worker waktu gagal secara terang-terangan sebelum API naik
        report_dead_workers()

        log(f"uvicorn listen di 0.0.0.0:{PORT}")
        api = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", PORT]
        )
        children.append(("api", api))
        return api.wait()
    finally:
        shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
