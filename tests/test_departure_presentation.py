from unittest.mock import Mock

import pytest

import departure_service
from agent_tools import ToolContext, execute_tool
from departure_presentation import passenger_delay_description
from models import Departure, DepartureBoard, DepartureWithDisruptions
from ptv import PTVConfig


@pytest.mark.parametrize("seconds,expected", [
    (0, "The estimate matches the schedule; this is not a guarantee of on-time operation."),
    (25, "Estimate is close to the schedule; timing may change."),
    (45, "Estimate is close to the schedule; timing may change."),
    (59, "Estimate is close to the schedule; timing may change."),
    (60, "About 1 minute late (estimated)."),
    (61, "About 1 minute late (estimated)."),
    (129, "About 2 minutes late (estimated)."),
    (245, "About 4 minutes late (estimated)."),
    (-30, "Slightly early (estimated)."),
    (-90, "About 2 minutes early (estimated)."),
    (None, "Timing comparison unavailable: a scheduled time or real-time estimate is missing."),
    (89, "About 1 minute late (estimated)."),
    (90, "About 2 minutes late (estimated)."),
    (149, "About 2 minutes late (estimated)."),
    (150, "About 3 minutes late (estimated)."),
    (-150, "About 3 minutes early (estimated)."),
])
def test_passenger_wording_uses_thresholds_and_nearest_minute_half_up(seconds, expected):
    assert passenger_delay_description(seconds) == expected


def test_tool_keeps_exact_129_second_diagnostics_and_timestamps_with_rounded_wording(monkeypatch):
    board = DepartureBoard(stop_id=1, departures=[DepartureWithDisruptions(departure=Departure(
        stop_id=1, scheduled_departure_utc="2026-11-17T22:58:00Z",
        estimated_departure_utc="2026-11-17T23:00:09Z"))])
    original = board.model_dump(mode="json")
    monkeypatch.setattr(departure_service, "get_departures_with_disruptions", Mock(return_value=board))
    context = ToolContext(config=PTVConfig("fake-id", "fake-key", "https://ptv.example"), known_stop_ids={1})
    result = execute_tool("get_departures_with_disruptions", '{"stop_id":1,"limit":5}', context)
    assert result["ok"] is True
    departure = result["board"]["departures"][0]["departure"]
    assert departure["delay_seconds"] == 129
    assert departure["delay_status"] == "late"
    assert departure["delay_description"] == "Estimated 129 seconds late relative to the schedule."
    assert departure["passenger_delay_description"] == "About 2 minutes late (estimated)."
    assert departure["scheduled_departure_utc"] == "2026-11-17T22:58:00Z"
    assert departure["estimated_departure_utc"] == "2026-11-17T23:00:09Z"
    assert departure["scheduled_local"] == "2026-11-18 09:58:00 AEDT"
    assert departure["estimated_local"] == "2026-11-18 10:00:09 AEDT"
    assert board.model_dump(mode="json") == original
