"""The Evidently path, exercised directly.

Gap this closes (`docs/TEST_GAP_MAP.md`, "Temuan"/sisa risiko): `_drift_evidently`
was only ever reached indirectly, and `check_drift` swallowed every exception into
the PSI fallback. So when the Evidently branch broke, the suite stayed green and
the only evidence was one print line nobody read.

It had in fact been broken from the start. `DataDriftPreset` expands into several
metrics whose order is not contractual, and the code read `metrics[0]`, which is
`DatasetDriftMetric` and carries no feature table at all. Every Evidently run
returned an empty `per_feature`, which the model card, the API and the dashboard
all read. Nothing caught it locally because this repo keeps its venv inside the
project directory and one of Evidently's transitive imports refuses to load from
the working directory, so local runs never took the branch. CI did, and had been
red on it for all six runs.

These tests therefore assert the branch actually RAN (`engine == "evidently"`),
not merely that a report came back, since a silent fallback would otherwise pass.
"""
from __future__ import annotations

import pandas as pd
import pytest

from common.config import settings
from ml.monitoring.drift import FEATURES, PSI_DRIFT_THRESHOLD, check_drift
from tests.conftest import calm_events, polluted_events


def _rows(events: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{f: e.get(f) for f in FEATURES} for e in events])


@pytest.fixture
def evidently_engine(monkeypatch):
    """Select the Evidently engine, or skip if this machine cannot import it.

    The skip is real and worth naming: when the venv lives inside the repo, nltk's
    working-directory guard blocks one of Evidently's transitive imports, so these
    cannot run locally. CI, Docker and Render keep the venv outside the tree and do
    run them. `exc_type` is explicit because that guard raises a plain ImportError,
    not ModuleNotFoundError, which pytest stops skipping on by default in 9.1.

    Tests that do not need Evidently deliberately skip this fixture so they still
    run everywhere.
    """
    pytest.importorskip(
        "evidently.metric_preset",
        exc_type=ImportError,
        reason="Evidently not importable here (venv inside the repo trips nltk's cwd guard)",
    )
    monkeypatch.setattr(settings, "drift_engine", "evidently")


def test_default_engine_is_psi_not_environment_dependent():
    """The detector is a decision in config, not whatever the environment allows."""
    assert settings.drift_engine == "psi"
    report = check_drift(
        _rows(calm_events("jaksel", settings.drift_window, seed=1)),
        _rows(polluted_events("jaksel", settings.drift_window, seed=2)),
    )
    assert report["engine"] == "psi"


def test_evidently_returns_a_full_feature_table(evidently_engine):
    """The regression itself: `per_feature` must cover every feature, not be empty."""
    window = settings.drift_window
    report = check_drift(
        _rows(calm_events("jaksel", window, seed=1)),
        _rows(polluted_events("jaksel", window, seed=2)),
    )

    assert report["engine"] == "evidently", (
        "silently fell back to PSI; this test is meaningless unless Evidently ran"
    )
    assert set(report["per_feature"]) == set(FEATURES)
    assert report["per_feature"]["pm25"]["drifted"] is True
    assert report["n_drifted"] == len(FEATURES)
    assert report["dataset_drift"] is True


def test_evidently_scores_are_psi_not_p_values(evidently_engine):
    """Pin the stat test, because the default one inverts what `score` means.

    Left to itself Evidently picks two-sample K-S at this sample size and reports the
    p-value as `drift_score`, so a drifted feature comes back as ~0.0 while a calm one
    comes back near 1.0. Every consumer here reads high-is-worse. A regime change of
    roughly 20 to 125 ug/m3 must therefore score far ABOVE the threshold, not at zero.
    """
    window = settings.drift_window
    report = check_drift(
        _rows(calm_events("jaksel", window, seed=1)),
        _rows(polluted_events("jaksel", window, seed=2)),
    )

    assert report["engine"] == "evidently"
    pm25 = report["per_feature"]["pm25"]
    assert pm25["score"] > PSI_DRIFT_THRESHOLD, (
        f"pm25 scored {pm25['score']}, which looks like a p-value, not a PSI value"
    )
    assert pm25["score"] > 1.0, "a full regime change should be nowhere near the threshold"


def test_evidently_report_keeps_the_shared_contract(evidently_engine, monkeypatch):
    """Both engines feed the same consumers, so both must return the same shape."""
    window = settings.drift_window
    reference = _rows(calm_events("jaksel", window, seed=1))
    current = _rows(polluted_events("jaksel", window, seed=2))

    with_evidently = check_drift(reference, current)
    monkeypatch.setattr(settings, "drift_engine", "psi")
    with_psi = check_drift(reference, current)

    assert with_evidently["engine"] == "evidently"
    assert with_psi["engine"] == "psi"
    assert set(with_evidently) == set(with_psi), "the two engines drifted apart in shape"
    for key in ("share_drifted", "n_drifted", "per_feature", "dataset_drift"):
        assert key in with_evidently
    assert set(with_evidently["per_feature"]) == set(with_psi["per_feature"])
    for feature, info in with_evidently["per_feature"].items():
        assert set(info) == set(with_psi["per_feature"][feature]) == {"score", "drifted"}


def test_evidently_failure_falls_back_to_psi(evidently_engine, monkeypatch):
    """Never-break philosophy: a broken Evidently degrades, it does not take the loop down."""
    import ml.monitoring.drift as drift_module

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated Evidently API change")

    monkeypatch.setattr(drift_module, "_drift_evidently", boom)
    monkeypatch.setattr(drift_module, "_evidently_warned", False)

    report = check_drift(
        _rows(calm_events("jaksel", settings.drift_window, seed=1)),
        _rows(polluted_events("jaksel", settings.drift_window, seed=2)),
    )

    assert report["engine"] == "psi"
    assert set(report["per_feature"]) == set(FEATURES)
    assert report["dataset_drift"] is True
