# PULSE — Jakarta Air Quality, after deploy

**Real-time air-quality intelligence for Jakarta: stream → online forecast →
anomaly detection → drift-triggered retraining → auto model card → LLM incident
card. One `docker compose up`.**

> **Flagship commitment:** PULSE is the single flagship project. The RAG résumé
> chatbot is parked until PULSE ships and is deployed publicly.

Most ML portfolios stop at *"I trained a model."* PULSE is about **what happens
after deploy**: the model keeps learning per-event, monitors itself for drift,
retrains and re-documents itself when the world changes, and narrates anomalies in
plain language. On real, local, streaming Jakarta data.

<!-- TODO: drop the demo GIF here once the dashboard MVP is green:
     spike button → chart jumps → incident card appears -->
<!-- ![PULSE demo](docs/demo.gif) -->

---

## The loop (this is the whole point)

```
OpenAQ + Weather API  ──(replay or live)──►  Redis Stream  aq.events
        │
        ▼
  ml/online/consumer.py
        ├─► river: forecast PM2.5 (horizon) + uncertainty band
        ├─► river: learn_one(x, y)        ⭐ TRUE online update, per event (not batch)
        └─► anomaly: HalfSpaceTrees → flag spikes
        │
        ├─► aq.predictions ──► API (WebSocket) ──► dashboard live chart
        ├─► aq.alerts ──► agent (Gemini) ──► aq.incidents ──► dashboard feed
        └─► drift (Evidently/PSI, windowed) ──► if drift ──► retrain
                                                          ──► registry + new model card
```

**What makes this not a tutorial:** `learn_one()` is called on *every* event (true
online learning, not batch retraining in disguise), and the **drift → retrain →
new version → new model card** loop closes back on itself automatically.

---

## Quickstart

```bash
cp .env.example .env          # defaults run in REPLAY mode — no keys, no internet
docker compose up --build     # redis + ingestion + ml + agent + api
```

Then:
- **Dashboard: http://localhost:3000** — the live ops center (Jakarta AQ, forecast
  chart, incident feed, demo controls). Served automatically by the `dashboard` service.
- API + interactive docs: **http://localhost:8000/docs**
- Health: **http://localhost:8000/health**
- Minimal reference harness (optional): open **`tools/devboard.html`**.

The dashboard (`Frontend_pulse/`) is a dc-runtime UI wired to the API over WebSocket +
REST. Data-shape contract lives in [`dashboard/FRONTEND_SPEC.md`](dashboard/FRONTEND_SPEC.md).

### Demo script (for recruiters)
1. `docker compose up` → charts start moving (replay streams synthetic Jakarta data).
2. Hit **Trigger Spike** (devboard or dashboard) → within ~1s: anomaly flagged →
   Gemini/template **incident card** appears → forecast band widens.
3. Let it run → drift accumulates → **auto-retrain** → new model version + model card
   in the registry. Show the version history. That's the "after deploy" story.

### No Docker? Run the brain offline
```bash
pip install -r requirements.txt
python -m scripts.smoke        # end-to-end pipeline check, no Redis needed
python -m ml.batch.baseline    # M1 batch baselines (comparison point)
pytest -q                      # unit tests
```

---

## Live vs Replay

| Mode | What it does | Needs |
|---|---|---|
| `replay` (default) | streams a historical/synthetic CSV as if live, time-compressed; supports on-demand spikes | nothing |
| `live` | polls real Jakarta AQ + weather | OpenAQ key optional (falls back to keyless Open-Meteo) |

Set `INGEST_MODE` in `.env`. **Replay is the demo weapon:** Jakarta isn't always
spiking, so replay lets you reproduce a spike→anomaly→incident moment on demand,
every time a recruiter is watching.

---

## Tech stack

`Python` · **`river`** (online ML — the one new thing) · `Redis Streams` (bus) ·
`FastAPI` + `WebSockets` · `Evidently` (drift, with PSI fallback) · `Gemini`
(incident cards, with deterministic template fallback) · local JSON model registry
(Supabase-ready) · `Docker Compose` · `Next.js` dashboard (separate, yours) ·
`GitHub Actions` (CI + scheduled retrain).

### Deliberately pruned for shipping (v1)
- **Redis Streams**, not Redpanda.
- **Local JSON registry**, not Supabase yet (swap only `ml/registry/registry.py`).
- **DVC** wired later; sample data generator stands in for now.
- The one genuinely new thing — **streaming + online learning (river)** — is where
  the effort goes.

---

## Repo structure

```
pulsev2/
├── docker-compose.yml      # one command runs the whole loop
├── common/                 # shared contract: config, schemas, redis bus, AQI health
├── ingestion/              # SERVICE 1 — producer (live) + replay (demo) + sample gen
├── ml/
│   ├── online/             # model (river SNARIMAX + baseline fallback), consumer, anomaly
│   ├── batch/baseline.py   # M1 batch comparison
│   ├── monitoring/drift.py # Evidently drift (+ PSI fallback)
│   ├── registry/           # versioned model registry (local JSON)
│   └── modelcard/          # auto model card per promotion
├── agent/                  # SERVICE 2 — Gemini incident cards (+ template fallback)
├── api/                    # SERVICE 3 — FastAPI REST + WebSocket + demo control
├── dashboard/              # SERVICE 4 — Next.js (YOU build this; see FRONTEND_SPEC.md)
├── tools/devboard.html     # throwaway harness to watch the backend live
├── scripts/smoke.py        # offline end-to-end pipeline check
├── tests/                  # unit tests
└── .github/workflows/      # ci.yml (lint+test) · retrain.yml (scheduled/manual)
```

---

## Milestones

- **M1 — Foundation:** ingestion + replay + baseline forecast with uncertainty. *(scaffold done)*
- **M2 — Online core:** river incremental updates + anomaly detection + live dashboard.
- **M3 — Lifecycle:** Evidently drift + auto-retrain + registry + model cards. *(loop wired)*
- **M4 — Agent + launch:** Gemini incident cards + alerting + deploy + README/GIF/build log.

Target: ship and deploy publicly in ~8–10 weeks. Don't let it become version four
of a portfolio that never launched.

---

## Build log

Keep decisions, trade-offs, and failures here — it's ~30% of the recruiter value.

- **2026-06-22** — Scaffolded the full walking skeleton: all services connect
  end-to-end in replay mode; offline smoke + unit tests green. river/Evidently/Gemini
  each have a graceful fallback so the loop never hard-fails. Next: build the dashboard
  MVP (live chart + status header + station selector + incident feed).
```
