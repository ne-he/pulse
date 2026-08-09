# Running PULSE for a demo

PULSE is not hosted. It is switched on when somebody is about to watch it and
switched off afterwards. This page is the runbook for that.

```bash
python -m deploy.demo
```

On Windows you can double-click **`demo.cmd`** instead. It installs the
requirements on first run and then does the same thing.

That is the whole setup. No Redis server, no Docker daemon, no second terminal
for the dashboard, no API keys. The bus runs inside the process
(`REDIS_URL=memory://`) and the API serves the dashboard itself, so everything
lives on one port.

---

## What happens when you run it

1. **Warm-up, about 17 seconds.** 600 frames (3,000 events) are pushed through
   the real `Engine` at full speed before the browser opens. This is a
   fast-forward through the same code the live workers run, not a fixture, so
   nothing on screen is fabricated.
2. **The browser opens at http://localhost:8000** with the dashboard already
   populated: a chart with history, a model that has learned from 3,000 events,
   real MAE and RMSE, several promoted model versions and a non-empty incident
   feed.
3. **The live stream continues** from the exact frame the warm-up stopped at, so
   the chart keeps moving forward instead of jumping back to the start.

Without the warm-up the first minute of a demo is a blank dashboard, which is
the worst possible minute to have somebody watching.

| Flag | Use it when |
|---|---|
| `--warm 0` | you want to show the cold-start behaviour on purpose |
| `--warm 1000` | you have time and want a longer chart history (about 27 s) |
| `--no-browser` | you are recording, or driving a second screen |
| `--speed 1200` | you want the feed to move twice as fast |
| `--fresh` | wipe the model registry so version numbers start at v1 again |
| `--port 9000` | port 8000 is taken |

---

## The three minute script

Open with what the thing is: **a live ops center for Jakarta air quality, where
the model learns from every single reading instead of being retrained in
batches.**

**1. The chart (about 40 s).**
Point at the solid line (actual PM2.5), the dashed line (forecast) and the
shaded band (uncertainty). Click between stations in the left rail. Say: five
stations, each with its own independent online model, forecasting one hour
ahead.

**2. Model health (about 30 s).**
Point at MAE / RMSE / EVENTS. Say: those error numbers are computed live from
one-step residuals, not from a training run. The event counter is how many
readings this model has learned from since it started.

**3. Press `[SPIKE]` (about 40 s). This is the moment.**
Within roughly a second: the chart jumps, an anomaly is flagged, and an incident
card appears in the feed naming the station, the size of the jump in sigmas, a
plausible cause and the forecast outlook. Say: that card is written from the
alert automatically, and the detector is not a threshold on the reading, it is
the model's own one-step surprise, so it adapts per station.

**4. Version history (about 40 s).**
Show the promoted versions. Say: when the feature distribution drifts far enough
the system retrains itself, promotes a new version and writes a model card, with
no human in the loop. Then point at a `Model retrained` card in the incident
feed. That is the "what happens after deploy" part that most portfolio projects
skip entirely.

Close with the honest bit, because it lands better than a claim: online learning
beats the persistence baseline here by 3 to 7 percent, not dramatically, and the
numbers are in the README including the station where it loses on RMSE.

---

## Before somebody is watching

- Run it once and leave it running. Warm-up is quick but it is not instant, and
  you do not want to spend it on a call.
- **Check the Gemini key.** With a working `GEMINI_API_KEY` the incident cards
  are LLM-written. Without one, or on a rate-limited key, the agent falls back to
  deterministic template cards. The fallback reads fine and the demo does not
  break, but the "agentic" line is stronger when the cards are generated. The
  agent stops retrying after three consecutive failures, so an exhausted key
  costs you nothing in latency, it just gives you templates.
- Have `http://localhost:8000/docs` ready in a second tab if the person is
  technical. The REST surface reads well.

## If something goes wrong

| Symptom | Cause and fix |
|---|---|
| `port already in use` | a previous run is still alive. `--port 9000`, or kill it |
| dashboard loads but stays empty | the API is up but no workers. Check the console for `worker '...' stopped` |
| `fakeredis` import error | `pip install -r requirements.txt` |
| incident feed empty at start | you passed `--warm 0`, or `--warm` below 600. Below 600 frames no drift retrain has happened yet and there is nothing to narrate |
| everything is slow | something else is using the CPU. Warm-up is single threaded and CPU bound |

## The other way to run it

`docker compose up --build` still works and is the closer analogue of a real
deployment: separate containers, a real Redis, the dashboard on port 3000. Use
it if somebody asks how this would actually ship. It needs the Docker daemon
running and a first build takes minutes, which is why it is not the demo path.
