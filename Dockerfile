# One image, many services. docker-compose runs different commands against it.
# Keeps the walking skeleton simple: build once, run ingestion/ml/agent/api from it.
#
# Image yang sama juga dipakai Hugging Face Space. Bedanya cuma di command:
# compose menimpa `command:` per layanan, sedangkan Space memakai CMD default di
# bawah, yang menyalakan kelimanya sekaligus lewat deploy/space_boot.py.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# redis-server dipasang di image supaya mode satu-container (Space) punya bus
# sendiri. Di compose paket ini nganggur: bus-nya dilayani service redis terpisah.
RUN apt-get update \
    && apt-get install -y --no-install-recommends redis-server \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Deps first for layer caching
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# App code (the whole monorepo; services pick their own entrypoint)
COPY common ./common
COPY ingestion ./ingestion
COPY ml ./ml
COPY agent ./agent
COPY api ./api
COPY deploy ./deploy

# data/ di-gitignore kecuali .gitkeep, jadi yang ikut ke image cuma foldernya. Itu
# cukup: replay membangkitkan sample sendiri kalau CSV-nya tidak ada, dan Registry
# bikin subfoldernya sendiri. chmod perlu karena keduanya menulis waktu runtime,
# sedangkan Space tidak selalu menjalankan container sebagai root.
COPY data ./data
RUN chmod -R 777 /app/data

# Port 7860 = default Hugging Face Spaces (lihat frontmatter README.md).
# Compose memetakan API-nya sendiri ke 8000, jadi baris ini tidak mengganggunya.
ENV PORT=7860
EXPOSE 7860

# Compose menimpa command ini per layanan. Space memakainya apa adanya:
# redis + ingestion + ml + agent + api hidup bareng di satu container.
CMD ["python", "deploy/space_boot.py"]
