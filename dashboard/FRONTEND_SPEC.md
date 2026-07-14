# PULSE Dashboard — Frontend Spec (build this part)

> This is an **ops / monitoring dashboard**, not a marketing landing page. Dark,
> data-dense, fast. Saturn Protocol vibe is welcome (deep charcoal bg, **Lava
> Orange `#FF4500`** accents) but **function first** — the hero is a live chart, not
> a hero image. Don't polish UI before the data flows.
>
> The backend is **done and running**. You only build the dashboard. Everything you
> need is the API + WebSocket contract below — no guessing.

---

## 0. TL;DR — what to build

A single-page (or 2-page) Next.js app that:
1. Opens **one WebSocket** to `ws://localhost:8000/ws`, receives a `snapshot` on
   connect, then live deltas (`observation`, `prediction`, `alert`, `incident`).
2. Renders a **live forecast chart** with an uncertainty band (the hero).
3. Shows a **status header**, a **station selector**, and a **live incident feed**.
4. (Phase 2) Adds **model health**, **drift monitor**, **model card viewer**,
   **version history**, and the **replay/demo control** (the secret weapon).

There's a throwaway reference implementation in [`tools/devboard.html`](../tools/devboard.html)
— open it after `docker compose up` to see the exact data shapes live. Don't ship
it; it's just proof the backend works and a shape reference.

---

## 1. Recommended stack

| Concern | Pick | Why |
|---|---|---|
| Framework | **Next.js (App Router) + TypeScript** | SSR for the About page, your existing plan |
| Styling | **Tailwind CSS** | fast, consistent dark theme |
| Charts | **Recharts** (easy) or **uPlot** (if it feels janky at high update rate) | area chart with shaded band = the hero |
| State | **Zustand** (or React context + reducer) | one store fed by the WS hook |
| Data fetch | native `fetch` for REST snapshots/history | no need for heavy libs |
| Icons | lucide-react | clean |

Keep it light. You do **not** need Redux, react-query, or a component kit. One WS
hook + one store + a handful of components.

### Env vars (`dashboard/.env.local`)
```
NEXT_PUBLIC_API_URL=http://localhost:8000
NEXT_PUBLIC_WS_URL=ws://localhost:8000/ws
```

---

## 2. The data contract (this is everything)

Base REST URL: `http://localhost:8000`. CORS is open. Interactive API docs live at
`http://localhost:8000/docs` once the backend is up.

### 2.1 WebSocket — `GET ws://localhost:8000/ws`

Every message is an envelope:
```ts
type WsMessage =
  | { type: "snapshot";    data: Snapshot }
  | { type: "observation"; data: Observation }   // a new raw reading
  | { type: "prediction";  data: Prediction }    // a new forecast (most frequent)
  | { type: "alert";       data: Alert }          // anomaly or drift flag
  | { type: "incident";    data: Incident };      // agent's natural-language card
```

On connect you immediately get **one** `snapshot` so the UI paints without waiting:
```ts
interface Snapshot {
  stations: Station[];
  predictions: Prediction[];   // latest per station
  observations: Observation[]; // latest per station
  incidents: Incident[];       // newest first, up to 20
  alerts: Alert[];             // newest first, up to 20
  model: ModelVersion | null;  // active model
  drift: DriftReport | null;   // last drift report
}
```
After that, handle each delta type and update your store. `prediction` arrives most
often (one per event per station) — that's what drives the live chart.

> Send anything (e.g. a `"ping"` string) periodically if you want; the server
> ignores inbound messages. Auto-reconnect on close (see devboard.html for the pattern).

### 2.2 TypeScript types (mirror the backend schemas exactly)

```ts
interface Station { id: string; name: string; lat: number; lon: number; }

interface Observation {
  station_id: string; station_name: string; ts: string; // ISO-8601 UTC
  pm25: number; pm10: number|null; no2: number|null; o3: number|null;
  temp: number|null; humidity: number|null; wind_speed: number|null;
  source: "live" | "replay";
}

interface Prediction {
  station_id: string; ts: string;
  pm25_now: number; aqi_now: number;
  horizon_min: number;                 // forecast lead time, e.g. 60
  pm25_forecast: number; aqi_forecast: number;
  forecast_lower: number; forecast_upper: number;   // the uncertainty band (pm2.5)
  category_now: string; category_forecast: string;  // "Good" | "Moderate" | "Unhealthy" ...
  trend: "up" | "down" | "flat";
  model_version: string;               // e.g. "v3"
  mae_rolling: number|null; rmse_rolling: number|null;  // live error
  n_seen: number;                      // events learned so far ("learning…")
}

interface Alert {
  alert_id: string; station_id: string; ts: string;
  type: "anomaly" | "drift";
  severity: "info" | "warning" | "critical";
  score: number; pm25: number|null;
  context: Record<string, any>;
}

interface Incident {
  incident_id: string; alert_id: string; station_id: string; ts: string;
  title: string; body: string;          // the narrative to render
  severity: "info" | "warning" | "critical";
  forecast_summary: string|null;
  generated_by: "gemini" | "template";  // show a small badge
}

interface ModelVersion {
  version: string; kind: string;        // "river"
  metrics: { mae: number|null; rmse: number|null; n_seen: number };
  reason: string; created_at: string; promoted: boolean;
}

interface DriftReport {
  engine: "evidently" | "psi";
  share_drifted: number; n_drifted: number;
  per_feature: Record<string, { score: number; drifted: boolean }>;
  dataset_drift: boolean;
}
```

### 2.3 REST endpoints

| Method | Path | Returns | Use for |
|---|---|---|---|
| GET | `/health` | `{status, redis, ws_clients}` | status dot |
| GET | `/stations` | `Station[]` | station selector |
| GET | `/stations/{id}/latest` | `{station, observation, prediction}` | initial paint per station |
| GET | `/predictions/latest` | `Prediction[]` | all stations at a glance |
| GET | `/predictions/{id}/history?limit=120` | `Prediction[]` | **seed the chart on load** |
| GET | `/observations/{id}/history?limit=120` | `Observation[]` | actual line history |
| GET | `/incidents?limit=30` | `Incident[]` | incident feed |
| GET | `/alerts?limit=30` | `Alert[]` | raw alert ticker |
| GET | `/model/active` | `ModelVersion` | model health panel |
| GET | `/model/versions` | `ModelVersion[]` | version history |
| GET | `/model/metrics` | per-station rolling MAE/RMSE/n_seen | model health |
| GET | `/modelcard/{version}` | `{version, markdown}` | model card viewer (render markdown) |
| GET | `/drift/status` | `{latest: DriftReport, threshold}` | drift monitor |
| POST | `/control` | `{ok, sent}` | **demo controls** |

### 2.4 Demo control — `POST /control`

Body = `ControlCommand`:
```ts
type ControlCommand = {
  action: "play" | "pause" | "set_speed" | "seek" | "trigger_spike";
  station_id?: string;   // for trigger_spike
  value?: number;        // speed for set_speed; pm2.5 magnitude for trigger_spike; index for seek
};
```
Examples:
```js
// inject a reproducible spike at Jaksel → anomaly → incident card within ~1s
fetch(`${API}/control`, {method:"POST", headers:{"Content-Type":"application/json"},
  body: JSON.stringify({ action:"trigger_spike", station_id:"jaksel", value:140 })});

fetch(`${API}/control`, {method:"POST", headers:{"Content-Type":"application/json"},
  body: JSON.stringify({ action:"pause" })});
```

---

## 3. Components to build (maps 1:1 to the feature table in hai.txt)

Health-band colors (use these so chart + badges match the backend AQI scale):
`Good #00E400 · Moderate #FFFF00 · Unhealthy(Sensitive) #FF7E00 · Unhealthy #FF0000 · Very Unhealthy #8F3F97 · Hazardous #7E0023`

### 🟢 MVP — build these first, in this order

**C1. LiveForecastChart** *(the hero)*
- X = time, Y = PM2.5. Three series: **actual** (solid line, from `observation`/
  `Prediction.pm25_now`), **forecast** (dashed line, `pm25_forecast`), and a
  **shaded uncertainty band** between `forecast_lower` and `forecast_upper`.
- Seed with `GET /predictions/{id}/history?limit=120` on mount; then append on each
  `prediction` WS message. Keep a rolling window (e.g. last 120 points) so it stays smooth.
- Acceptance: band visibly brackets the forecast line; chart updates live without flicker.

**C2. StatusHeader**
- Big current PM2.5 + AQI number, colored by `category_now`, trend arrow (`trend`),
  and a badge: `Forecast +{horizon_min}min: {category_forecast}`.
- Source: latest `Prediction` for the selected station.

**C3. StationSelector**
- Pills/tabs for the 5 Jakarta stations (`GET /stations`), each tinted by its current
  health band. Switching station re-points the chart + header.

**C4. IncidentFeed**
- Real-time list, newest on top. Each card: `title`, `body`, severity color stripe,
  and a small `generated_by` badge (`gemini` / `template`). Animate new items in.
- Seed from snapshot `incidents`; append on `incident` WS messages.

### 🟡 Phase 2 — the differentiators (this is what recruiters rarely see)

**C5. ModelHealthPanel** — active `model_version`, live rolling **MAE/RMSE**, and a
pulsing "learning…" indicator driven by `n_seen` increasing. Source: `/model/metrics`
+ live `prediction` deltas.

**C6. DriftMonitor** — `/drift/status`: show `share_drifted`, a per-feature list
(which features drifted), engine badge, and a small **drift→retrain timeline**
(each `alert` with `type:"drift"` = a retrain event).

**C7. ModelCardViewer** — pick a version, `GET /modelcard/{version}`, render the
returned markdown (use `react-markdown`).

**C8. VersionHistory** — `GET /model/versions`: table of version, MAE/RMSE, reason,
`created_at` ("promoted at"). Clicking a row opens its model card (C7).

**C9. ReplayControl** *(⭐ secret weapon)* — play / pause / speed slider / **"Trigger
Spike" button** per station, all via `POST /control`. This makes the
spike→anomaly→incident moment reproducible on demand. **Build this — it's the
highest-ROI feature for the demo.**

### 🔵 Polish — last

**C10.** Incident detail modal, **About/Architecture page** (embed the README
diagram; doubles as the recruiter explainer), responsive/mobile, full dark mode,
Lighthouse 90+.

---

## 4. Suggested dashboard/ structure

```
dashboard/
├── app/
│   ├── page.tsx              # main ops view (MVP components)
│   ├── about/page.tsx        # architecture explainer (SSR)
│   └── layout.tsx
├── components/
│   ├── LiveForecastChart.tsx
│   ├── StatusHeader.tsx
│   ├── StationSelector.tsx
│   ├── IncidentFeed.tsx
│   ├── ModelHealthPanel.tsx
│   ├── DriftMonitor.tsx
│   ├── ModelCardViewer.tsx
│   ├── VersionHistory.tsx
│   └── ReplayControl.tsx
├── lib/
│   ├── usePulseSocket.ts     # the single WS hook → store
│   ├── store.ts              # zustand store
│   ├── api.ts                # REST helpers
│   └── health.ts             # AQI color/category helpers (mirror common/health.py)
├── types.ts                  # paste section 2.2 here
└── .env.local
```

### The one hook everything hangs off (`usePulseSocket.ts`)
```ts
// pseudocode — open WS, on "snapshot" hydrate store, on deltas update store,
// auto-reconnect on close. Components read from the store; they never touch the WS.
```

---

## 5. Definition of done (MVP)
- [ ] `docker compose up` backend running, dashboard at `localhost:3000`.
- [ ] WS connects, status dot goes green, snapshot paints instantly.
- [ ] Live chart shows actual + forecast + uncertainty band, updating in real time.
- [ ] Switching stations works; header reflects the selected station.
- [ ] Clicking **Trigger Spike** produces an incident card in the feed within ~2s.
- [ ] Looks intentional in dark mode on a laptop screen (mobile can come in Polish).

> When MVP is green, record a 20–30s GIF (spike button → chart jumps → incident card
> appears) and drop it in the README. That GIF is 30% of the recruiter value.
```
