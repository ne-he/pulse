"""Consumer — the beating heart of PULSE. Reads events, forecasts, learns online,
flags anomalies, watches for drift, and closes the loop by retraining + minting a
new model version and card. Emits predictions and alerts back onto the bus.

    aq.events ─► predict (with uncertainty) ─► learn_one() ─► anomaly score
              ─► emit aq.predictions / aq.alerts
              ─► windowed drift check ─► if drift ─► register version + model card
"""
from __future__ import annotations

import uuid

import pandas as pd

from common.config import Streams, settings
from common.health import aqi_category, pm25_to_aqi
from common.redis_bus import StreamReader, get_client, publish
from common.schemas import Alert, Prediction
from ml.modelcard.generate import generate_card
from ml.monitoring.drift import FEATURES, check_drift
from ml.online.anomaly import AnomalyDetector
from ml.online.model import StationModel, exog_features
from ml.registry.registry import Registry


class Engine:
    def __init__(self):
        self.models: dict[str, StationModel] = {}
        self.detectors: dict[str, AnomalyDetector] = {}
        self.registry = Registry()

        # drift bookkeeping (across all stations)
        self.reference_rows: list[dict] = []
        self.current_rows: list[dict] = []
        self.events_since_check = 0

        self.version = self._bootstrap_version()

    def _bootstrap_version(self) -> str:
        active = self.registry.active()
        if active:
            return active["version"]
        rec = self.registry.register(
            metrics={"mae": None, "rmse": None, "n_seen": 0},
            kind=settings.model_kind, reason="initial bootstrap",
        )
        self.registry.save_card(rec["version"], generate_card(rec))
        print(f"[ml] bootstrapped model {rec['version']}")
        return rec["version"]

    def _model(self, sid: str) -> StationModel:
        if sid not in self.models:
            self.models[sid] = StationModel(sid)
            self.detectors[sid] = AnomalyDetector(sid)
        return self.models[sid]

    # ── per-event processing ────────────────────────────────────────────
    def process(self, client, event: dict) -> None:
        sid = event["station_id"]
        model = self._model(sid)
        detector = self.detectors[sid]
        y = float(event["pm25"])
        exog = exog_features(event)

        # forecast with CURRENT model (before learning from this event)
        point, lower, upper = model.forecast(exog)
        score, is_anomaly = detector.score(event)

        # TRUE online update
        model.observe(y, exog)

        aqi_now = pm25_to_aqi(y)
        aqi_fc = pm25_to_aqi(point)
        pred = Prediction(
            station_id=sid, ts=event.get("ts"),
            pm25_now=round(y, 2), aqi_now=aqi_now,
            horizon_min=model.horizon_minutes(),
            pm25_forecast=round(point, 2), aqi_forecast=aqi_fc,
            forecast_lower=round(lower, 2), forecast_upper=round(upper, 2),
            category_now=aqi_category(aqi_now)[0],
            category_forecast=aqi_category(aqi_fc)[0],
            trend=model.trend(point),
            model_version=self.version,
            mae_rolling=round(model.mae, 3) if model.mae is not None else None,
            rmse_rolling=round(model.rmse, 3) if model.rmse is not None else None,
            n_seen=model.n,
        )
        publish(client, Streams.PREDICTIONS, pred)

        if is_anomaly:
            alert = Alert(
                alert_id=str(uuid.uuid4())[:8], station_id=sid, ts=event.get("ts"),
                type="anomaly",
                severity="critical" if aqi_now >= 151 else "warning",
                score=round(score, 3), pm25=round(y, 2),
                context={
                    "station_name": event.get("station_name"),
                    "aqi_now": aqi_now, "category": aqi_category(aqi_now)[0],
                    "wind_speed": event.get("wind_speed"),
                    "forecast": round(point, 2),
                    "category_forecast": aqi_category(aqi_fc)[0],
                    "horizon_min": model.horizon_minutes(),
                },
            )
            publish(client, Streams.ALERTS, alert)
            print(f"[ml] 🚨 anomaly @ {sid} score={score:.2f} pm2.5={y:.1f}")

        self._track_drift(client, event)

    # ── drift → retrain (closed loop) ───────────────────────────────────
    def _track_drift(self, client, event: dict) -> None:
        row = {f: event.get(f) for f in FEATURES}
        if len(self.reference_rows) < settings.drift_window:
            self.reference_rows.append(row)
            return  # still building the reference baseline

        self.current_rows.append(row)
        if len(self.current_rows) > settings.drift_window:
            self.current_rows.pop(0)

        self.events_since_check += 1
        if self.events_since_check < settings.drift_window or len(self.current_rows) < settings.drift_window:
            return
        self.events_since_check = 0

        report = check_drift(pd.DataFrame(self.reference_rows), pd.DataFrame(self.current_rows))
        print(f"[ml] drift check: share={report['share_drifted']} dataset_drift={report['dataset_drift']}")
        if report["dataset_drift"]:
            self._retrain(client, report)

    def _retrain(self, client, drift_report: dict) -> None:
        """Closed loop: drift → adopt current online state as a new version + card.

        With online learning the weights already adapt continuously; 'retrain' here
        snapshots that adapted state into an immutable, documented version and
        resets the drift reference so monitoring tracks the new regime."""
        maes = [m.mae for m in self.models.values() if m.mae is not None]
        rmses = [m.rmse for m in self.models.values() if m.rmse is not None]
        metrics = {
            "mae": round(sum(maes) / len(maes), 3) if maes else None,
            "rmse": round(sum(rmses) / len(rmses), 3) if rmses else None,
            "n_seen": sum(m.n for m in self.models.values()),
        }
        rec = self.registry.register(
            metrics=metrics, kind=settings.model_kind,
            reason=f"drift-triggered retrain (share={drift_report['share_drifted']})",
        )
        self.registry.save_card(rec["version"], generate_card(rec, drift_report))
        self.version = rec["version"]

        # narrate the drift event too (agent picks this up)
        alert = Alert(
            alert_id=str(uuid.uuid4())[:8], station_id="ALL", type="drift",
            severity="warning", score=drift_report["share_drifted"],
            context={"new_version": rec["version"], "drift": drift_report, "metrics": metrics},
        )
        publish(client, Streams.ALERTS, alert)

        # new regime becomes the reference
        self.reference_rows = list(self.current_rows)
        self.current_rows = []
        print(f"[ml] 🔁 drift→retrain → promoted {rec['version']} (mae={metrics['mae']})")


def run() -> None:
    client = get_client()
    engine = Engine()
    reader = StreamReader(client, [Streams.EVENTS])
    print(f"[ml] consumer online (model={settings.model_kind}, active={engine.version})")
    while True:
        for _stream, _id, event in reader.read(block_ms=5000):
            try:
                engine.process(client, event)
            except Exception as exc:  # noqa: BLE001 — one bad event must not kill the loop
                print(f"[ml] process error: {exc}")


if __name__ == "__main__":
    run()
