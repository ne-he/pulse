"""Service 1 — Ingestion. Pulls air-quality + weather data and pushes typed
events onto the Redis stream. Two modes: `live` (OpenAQ + Open-Meteo) and
`replay` (historical/synthetic data streamed as if live — the demo weapon)."""
