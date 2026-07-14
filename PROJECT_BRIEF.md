# PULSE — Project Brief

> **Knowledge base for the "robot" (project chatbot) and for human readers.**
> This document explains what PULSE is, why it exists, how it works, and what it
> demonstrates — in plain language first, technical depth second. Anything the robot
> tells a visitor should be grounded in this file. Read the **Robot Guidance** section
> at the bottom before answering as the project.

---

## 1. One-liner

**PULSE is a real-time air-quality intelligence platform for Jakarta that shows what happens to a machine-learning model *after* it's deployed** — it forecasts pollution, learns from every new reading, watches itself for drift, retrains and re-documents itself automatically, and explains air-quality spikes in plain language.

## 2. Elevator pitch (30 seconds)

Most ML portfolio projects stop at *"I trained a model and got X% accuracy."* PULSE is about the hard, real part that comes next: keeping a model **alive in production**. Air-quality readings from Jakarta stream in continuously; a model forecasts the next hour of PM2.5 with an uncertainty range and **updates itself on every single reading** (true online learning, not periodic batch retraining). When pollution spikes unexpectedly, an anomaly detector flags it and an LLM agent writes a short, human-readable incident report. When the data pattern shifts over time (drift), the system **retrains itself automatically** and publishes a fresh "model card" documenting the new version. Everything runs with one command and streams to a live dashboard. It's a complete, self-maintaining ML system built on a real, local problem — air pollution in one of the world's most polluted major cities.

## 3. What is PULSE, plainly?

Imagine a control room for Jakarta's air. On the wall is a live chart of pollution for each part of the city, a forecast of where it's heading in the next hour, and a feed of plain-English alerts like:

> *"PM2.5 rose 40% at Jakarta Selatan, likely from traffic combined with low wind. Forecast: Unhealthy for the next 6 hours."*

Behind that control room is a machine-learning system that never stops learning, checks its own health, and fixes itself when the world changes. PULSE is that whole thing — the brain, the plumbing, and the control room — packaged so anyone can run it with a single command.

## 4. Why it exists (the problem)

- **Jakarta air quality is a real, local, high-stakes problem.** It's one of the most polluted major cities in the world; residents genuinely care about "is it safe outside in the next few hours?"
- **Air-quality data is naturally streaming.** New readings arrive continuously — the perfect setting to demonstrate real-time / online machine learning rather than a static dataset.
- **The portfolio gap it fills:** almost every junior ML portfolio proves "I can train a model." Very few prove "I can *operate* a model in production" — monitoring, drift, automated retraining, versioning, documentation, alerting. PULSE deliberately targets that gap. Its tagline is literally *"Jakarta Air Quality, after deploy."*

## 5. What it does — the closed loop (the heart of the project)

```
OpenAQ + Weather API  ──(live or replay)──►  Redis Stream: aq.events
        │
        ▼
  Online ML consumer
        ├─► forecast next-hour PM2.5 + uncertainty band   (river SNARIMAX)
        ├─► learn_one(x, y)  on EVERY event  ⭐ true online update, not batch
        └─► anomaly detection → flag sudden spikes         (HalfSpaceTrees)
        │
        ├─► predictions ──► API (WebSocket) ──► live dashboard chart
        ├─► alerts ──► Gemini agent ──► plain-language incident card ──► dashboard feed
        └─► drift monitor (windowed) ──► if drift detected ──► auto-retrain
                                                            ──► new model version
                                                            ──► new auto model card
```

**Why this loop is the whole point:** it *closes back on itself*. Drift → retrain → new version → new documentation → keep serving, with no human in the loop. That self-maintaining cycle is what separates a production ML system from a notebook.

## 6. Architecture (four services + a bus)

PULSE is a small distributed system, not one script. Services communicate over **Redis Streams** (a message bus). One `docker compose up` starts everything.

| Service | Folder | Responsibility |
|---|---|---|
| **Ingestion** | `ingestion/` | Pulls Jakarta air-quality + weather data and pushes events to the bus. Two modes: `live` (real APIs) or `replay` (streams a historical/synthetic CSV as if live, time-compressed — the demo engine). |
| **Online ML** | `ml/` | The brain. Per-station online forecaster (`ml/online/model.py`), anomaly detector (`ml/online/anomaly.py`), drift monitor (`ml/monitoring/drift.py`), versioned model registry (`ml/registry/`), and auto model-card generator (`ml/modelcard/`). |
| **Agent** | `agent/` | Turns machine-readable alerts into short natural-language **incident cards** using Gemini (with a deterministic template fallback so it works with no API key). |
| **API** | `api/` | FastAPI backend exposing REST endpoints + a **WebSocket** that streams live updates to the dashboard, plus a `POST /control` endpoint for demo controls. |
| **Dashboard** | `Frontend_pulse/` | The live ops UI (dark theme, "Saturn Protocol" Lava-Orange accents). Live forecast chart, status header, station selector, incident feed, and demo controls — wired to the backend over WebSocket + REST. |

**The bus contract** (Redis Stream names): `aq.events` (raw readings) → `aq.predictions` (forecasts) → `aq.alerts` (anomaly/drift flags) → `aq.incidents` (agent cards) → `aq.control` (demo commands).

## 7. The core innovation (what makes it "not a tutorial")

1. **True online learning.** `model.learn_one(x, y)` is called on *every* event. The model improves continuously, per-reading — not by re-running a batch job on a schedule. Each of the 5 stations keeps its own independent model.
2. **Closed-loop drift → retrain → re-document.** The system monitors its own input distribution; when it drifts past a threshold, it retrains, promotes a new version to the registry, and auto-generates a new model card — automatically.
3. **Auto model cards.** Every model promotion produces a documentation card (metrics, reason, timestamp) — governance built in, not bolted on.
4. **LLM incident narration.** An agent turns cold anomaly scores into calibrated, plain-language explanations grounded in the actual data ("likely traffic + low wind"), never inventing numbers.
5. **Reproducible demo weapon (replay mode).** Jakarta isn't always spiking, so a recruiter opening the dashboard on a calm day would see nothing dramatic. Replay mode streams historical/synthetic data and lets you **trigger a pollution spike on demand** — so the spike → anomaly → incident-card moment is reproducible every single time someone is watching.
6. **Never-break philosophy.** river, Evidently, and Gemini each have a graceful fallback (baseline model, PSI drift, template card). The system degrades instead of crashing — a production mindset.

## 8. Tech stack (and why each)

| Concern | Choice | Why |
|---|---|---|
| Online ML | **river** (SNARIMAX + weather/time exog features) | The one genuinely new skill; enables per-event incremental learning |
| Streaming bus | **Redis Streams** | Simple, one container; real streaming without the weight of Kafka/Redpanda |
| API + realtime | **FastAPI + WebSockets** | Async REST + push updates to the dashboard |
| Drift monitoring | **Evidently** (with a PSI fallback) | Industry-standard drift reports |
| LLM agent | **Gemini** (`gemini-2.0-flash`, template fallback) | Generous free tier; writes incident cards |
| Model registry | **Local JSON** (Supabase-ready) | Ships now; swap one file to go cloud later |
| Packaging | **Docker Compose** | One command runs the whole system — recruiters can't run a notebook |
| CI / automation | **GitHub Actions** | `ci.yml` (lint + test) and `retrain.yml` (scheduled/manual retrain) |
| Dashboard | Dark ops UI (dc-runtime; Next.js spec available) | Function-first live monitoring view |

**Deliberately pruned for v1 shipping:** Redis Streams (not Redpanda), local JSON registry (not Supabase yet), synthetic data generator standing in for DVC. The effort concentrates on the one new thing: streaming + online learning.

## 9. Key features

**Backend / ML capabilities**
- Per-station next-hour PM2.5 forecast (horizon 60 min) with a 95% uncertainty band.
- True per-event online learning; live rolling MAE/RMSE per station.
- Anomaly detection on sudden spikes (score threshold 0.85).
- Drift monitoring over a rolling window; automatic retraining when drift crosses threshold.
- Versioned model registry + auto-generated model card per promotion.
- LLM incident cards for anomalies and drift/retrain events.
- US-AQI derivation and health categories (Good → Hazardous).

**Dashboard features**
- 🟢 *MVP:* live forecast chart with uncertainty band (the hero), status header (current AQI + trend + forecast badge), station selector (5 Jakarta stations), live incident feed.
- 🟡 *Differentiators:* model-health panel (live MAE/RMSE + "learning…" indicator), drift monitor with drift→retrain timeline, model-card viewer, version history.
- ⭐ *Secret weapon:* replay/demo control — play / pause / speed / **Trigger Spike** button, making the wow-moment reproducible on command.

## 10. Data & coverage

- **5 Jakarta stations:** Jakarta Selatan (`jaksel`), Jakarta Pusat (`jakpus`), Jakarta Barat (`jakbar`), Jakarta Timur (`jaktim`), Jakarta Utara (`jakut`).
- **Signals per reading:** PM2.5 (the forecast target), PM10, NO2, O3, plus weather (temperature, humidity, wind speed) used as exogenous model features.
- **Data source:** built to consume real **OpenAQ** (air quality) + **Open-Meteo** (weather) for Jakarta in live mode. Ships with a **synthetic Jakarta generator** for zero-setup, reproducible demos (replay mode is the default).

## 11. Status & how to run it

- **Status (as of 2026-07):** backend is **complete and runs end-to-end**; offline smoke test, unit tests, and lint all pass. The dashboard UI is built and wired to the backend. **Not yet publicly deployed** — next step is deploy + portfolio publish.
- **Run it:**
  ```bash
  cp .env.example .env        # defaults = replay mode, no keys, no internet
  docker compose up --build   # redis + ingestion + ml + agent + api (+ dashboard)
  ```
  Dashboard → `http://localhost:3000`, API docs → `http://localhost:8000/docs`.
- **Demo script:** start it → charts move → hit **Trigger Spike** → within ~1s an anomaly is flagged and an incident card appears → let it run → drift accumulates → auto-retrain → new model version + card in the registry.

## 12. What it demonstrates (recruiter value)

- **Real-time / streaming ML** — event-driven pipeline over a message bus.
- **Production ML lifecycle / MLOps** — monitoring, drift detection, automated retraining, model registry, model cards, CI.
- **Time-series forecasting** with uncertainty quantification.
- **Online / incremental learning** — a genuinely uncommon skill in junior portfolios.
- **Agentic AI** — an LLM integrated as a functional component, not a gimmick.
- **Systems / engineering maturity** — multi-service architecture, Docker, graceful degradation, one-command reproducibility.
- **The narrative:** *"I run ML in production, not just in a notebook."*

## 13. Honest limitations (state these plainly — they build credibility)

- The default demo runs on **synthetic** Jakarta data (realistic, but generated) so it works with zero setup; a real-data live mode (OpenAQ + Open-Meteo) is implemented but optional.
- **Not yet publicly deployed** at the time of writing — it runs locally via Docker.
- Model registry is local JSON (not yet a hosted DB); DVC is planned, not wired.
- The forecasting model is a solid baseline (SNARIMAX), chosen for online-learning fit rather than leaderboard accuracy — the point of the project is the *lifecycle around* the model, not squeezing maximum accuracy.

## 14. Links

- **GitHub:** https://github.com/ne-he/pulse
- **Live preview:** `TODO — Nemi's Garage embed / deployed URL`
- **Built by:** Nehemiah (Data Science student, Jakarta) — part of the **"Nemi's Garage"** portfolio.

---

## 15. FAQ (use these to answer visitors)

**Q: What is PULSE in one sentence?**
A real-time Jakarta air-quality platform that demonstrates the full life of an ML model *after* deployment — online forecasting, self-monitoring, automatic retraining, and plain-language incident reports.

**Q: What problem does it solve?**
Two things at once: it forecasts Jakarta's air quality with uncertainty, and — more importantly for a portfolio — it shows how to *operate and maintain* an ML model in production, the part most projects skip.

**Q: What's the most technically interesting part?**
True online learning: the model updates on every single reading rather than in scheduled batches, and the whole drift → retrain → re-document cycle closes automatically without a human.

**Q: Is this real Jakarta data?**
It's built to consume real OpenAQ + Open-Meteo data for 5 Jakarta stations in live mode. The default demo uses a realistic synthetic generator so it runs instantly with no API keys — and so a pollution spike can be reproduced on demand for demos.

**Q: What is "online learning" and why does it matter?**
Instead of retraining a model on a big dataset every so often, the model learns incrementally from each new data point as it arrives. It matters because real-world data streams continuously and patterns change — online learning adapts immediately.

**Q: What is "drift" and what happens when it's detected?**
Drift means the incoming data's pattern has shifted away from what the model learned. PULSE watches for it; when drift crosses a threshold, it automatically retrains the model, promotes a new version, and generates fresh documentation (a model card).

**Q: What does the LLM/agent actually do?**
When an anomaly or drift is detected, the agent (Gemini) writes a short, calibrated, plain-language incident card — what changed, a likely cause grounded in the data, and the forecast outlook. It never invents numbers and hedges causes with "likely/possibly."

**Q: What's the tech stack?**
Python, river (online ML), Redis Streams, FastAPI + WebSockets, Evidently (drift), Gemini (incident cards), Docker Compose, GitHub Actions, and a dark ops dashboard.

**Q: Is it deployed?**
It runs end-to-end locally with one command. Public deployment is the current next step.

**Q: How long did it take / who built it?**
Built solo by Nehemiah, a Data Science student in Jakarta, as his flagship portfolio project — scoped to ship in roughly 8–10 weeks.

**Q: Why forecast with SNARIMAX instead of a fancy deep model?**
The project's value is the *production lifecycle around* the model, not leaderboard accuracy. SNARIMAX fits online, per-event learning cleanly and stays interpretable; the architecture can swap in a stronger forecaster without changing the loop.

**Q: What would you improve next?**
Public deployment, a hosted model registry (Supabase), wiring DVC for data versioning, and evaluating stronger online forecasters — plus the Phase-2 dashboard panels (drift timeline, model-card viewer, version history).

**Q: What makes this different from a tutorial project?**
The self-closing loop: per-event learning + automatic drift-triggered retraining + auto model cards + LLM narration, all running as a multi-service system on a real local problem. Most portfolios stop at training a model; this one starts where they stop.

---

## 16. Robot guidance (read before answering as the project)

**Persona:** You are the friendly, precise representative of *PULSE*, a project by Nehemiah. Speak like a sharp engineer explaining their own work — confident, concrete, no fluff. You can answer in **English or Indonesian**, matching the visitor's language.

**Rules:**
- Ground every claim in this brief. If asked something not covered, say you're not sure rather than inventing details (especially specific metrics, accuracy numbers, or dates).
- **Be honest about status:** the backend is complete and runs locally; the default demo uses synthetic data; it is **not yet publicly deployed** (update this once it is).
- Explain technical concepts simply first (online learning, drift, anomaly detection), then offer more depth if the visitor wants it.
- Lead with the project's core story when relevant: *"what happens to an ML model after you deploy it."*
- It's fine to talk about Nehemiah in the third person as the builder, and to point visitors to the GitHub repo and live preview links.
- Keep answers short by default (2–5 sentences); expand only when asked.
- Never give medical advice beyond standard AQI category meanings.

**One-line summary to fall back on:**
> PULSE is Nehemiah's real-time Jakarta air-quality platform that shows the full life of an ML model after deployment: it forecasts pollution with uncertainty, learns from every reading, detects anomalies and drift, retrains and re-documents itself automatically, and narrates incidents in plain language — all running as a one-command, multi-service system.
