"""The demo controls have to actually reach the replay engine.

`[SPIKE]` is the single most important control in the project: the README's demo
script tells a recruiter to press it and watch an incident appear. It shipped
broken. The replay loop polled the control stream with `XREAD ... $` and no
BLOCK, which can never return anything, so play, pause, set_speed, seek and
trigger_spike were all silently dropped.

Nothing caught it because no test ever exercised the control path, and by hand
it looks like "the replay just kept going", which is also what a working system
looks like when the button does nothing.
"""
from __future__ import annotations

import json

import pytest

from common.config import Streams
from common.redis_bus import publish
from common.schemas import STATIONS, AQEvent, ControlCommand
from ingestion.replay import SPIKE_DECAY, SPIKE_FRAMES, _apply_spike, control_start_offset
from ml.online.anomaly import AnomalyDetector
from tests.conftest import calm_events

fakeredis = pytest.importorskip("fakeredis", reason="in-process bus for the control test")


@pytest.fixture
def client():
    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


def _drain(client, offset):
    """Exactly the non-blocking read the replay loop performs each frame."""
    seen = []
    resp = client.xread({Streams.CONTROL: offset}, count=20)
    for _stream, messages in resp or []:
        for msg_id, fields in messages:
            offset = msg_id
            seen.append(json.loads(fields["data"]))
    return seen, offset


def test_dollar_offset_can_never_see_a_command(client):
    """Pin the actual defect, so nobody reintroduces '$' thinking it is harmless."""
    publish(client, Streams.CONTROL, ControlCommand(action="trigger_spike"))
    assert client.xlen(Streams.CONTROL) == 1
    seen, _ = _drain(client, "$")
    assert seen == [], "if this ever returns a command, the '$' explanation is wrong"


def test_control_command_reaches_the_replay_loop(client):
    """A spike pressed after startup must be visible on the next poll."""
    offset = control_start_offset(client)          # cold stream
    publish(client, Streams.CONTROL, ControlCommand(
        action="trigger_spike", station_id="jaksel", value=150.0))

    seen, offset = _drain(client, offset)
    assert len(seen) == 1, "the spike command was dropped"
    assert seen[0]["action"] == "trigger_spike"
    assert seen[0]["station_id"] == "jaksel"
    assert seen[0]["value"] == 150.0


def test_commands_are_consumed_once_not_replayed(client):
    """The offset must advance, or every frame re-fires every past command."""
    offset = control_start_offset(client)
    publish(client, Streams.CONTROL, ControlCommand(action="pause"))
    first, offset = _drain(client, offset)
    second, offset = _drain(client, offset)
    assert len(first) == 1
    assert second == [], "a consumed command came back on the next poll"

    publish(client, Streams.CONTROL, ControlCommand(action="play"))
    third, _ = _drain(client, offset)
    assert [c["action"] for c in third] == ["play"]


def test_injected_spike_alarms_once_not_on_its_own_fade():
    """The demo must not invent incidents about its own synthetic decay.

    Pressing [SPIKE] should read as one pollution episode: a sharp onset that
    raises exactly one alert, then a fade quiet enough that the detector treats
    it as a return to normal. A steep fade turns every decay step into a fresh
    "PM2.5 drop" card that buries the spike card the demo is there to show.
    """
    base = calm_events("jakbar", 120)
    det = AnomalyDetector("jakbar")
    for event in base[:60]:                       # settle on a normal baseline
        det.score(event)

    bump, left, flags = 170.0, SPIKE_FRAMES, []
    for step, event in enumerate(base[60:]):
        payload = AQEvent(station_name=STATIONS["jakbar"]["name"], **{
            k: event[k] for k in ("station_id", "ts", "pm25", "wind_speed")})
        if left > 0:
            payload = _apply_spike(payload, bump)
            bump *= SPIKE_DECAY
            left -= 1
        if det.score(payload.model_dump())[1]:
            flags.append(step)

    assert flags == [0], (
        f"expected one alert on the onset, got {len(flags)} at steps {flags[:6]}"
    )


def test_spike_overlay_fades_out_instead_of_cutting_off():
    """`max(reading, bump)` must hand back to the real feed with no visible step."""
    event = AQEvent(station_id="jakbar", station_name="Jakarta Barat",
                    pm25=18.0, wind_speed=2.4)
    loud = _apply_spike(event, 170.0)
    assert loud.pm25 == 170.0
    assert loud.wind_speed <= 0.6, "an episode should look stagnant, not breezy"

    faded = _apply_spike(event, 12.0)             # bump now BELOW the real reading
    assert faded.pm25 == 18.0, "a spent bump must not pull the reading down"


def test_history_before_startup_is_ignored(client):
    """Commands issued before this worker existed must not replay on boot.

    That was the point of '$' and it is worth keeping: a worker restarting into
    a stream full of old pause commands should not pause itself.
    """
    publish(client, Streams.CONTROL, ControlCommand(action="pause"))
    publish(client, Streams.CONTROL, ControlCommand(action="set_speed", value=50.0))

    offset = control_start_offset(client)          # start AFTER the history
    seen, offset = _drain(client, offset)
    assert seen == [], "stale commands from before startup were replayed"

    publish(client, Streams.CONTROL, ControlCommand(action="play"))
    seen, _ = _drain(client, offset)
    assert [c["action"] for c in seen] == ["play"]
