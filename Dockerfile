# One image, many services. docker-compose runs different commands against it.
# Keeps the walking skeleton simple: build once, run ingestion/ml/agent/api from it.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

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

# Default command is overridden per-service in docker-compose.yml
CMD ["python", "-c", "print('PULSE base image — override the command in docker-compose')"]
