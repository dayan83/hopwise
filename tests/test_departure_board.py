from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import pytest

import departure_board
import departure_service
import ptv


@pytest.fixture
def config(monkeypatch):
    config = ptv.PTVConfig("fake-id", "fake-key", "https://ptv.example")
    monkeypatch.setattr(departure_board.PTVConfig, "from_env", lambda: config)
    return config


def station(name="Richmond", stop_id=1):
    return {"stop_name": name, "stop_id": stop_id, "stop_suburb": "Richmond", "route_type": 0}


def test_full_search_service_and_console_flow(config, monkeypatch, capsys):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/v3/search/Richmond":
            return httpx.Response(200, json={"stops": [station("East Richmond", 99), station("Richmond Station")]})
        assert request.url.path == "/v3/departures/route_type/0/stop/1"
        assert request.url.params["expand"] == "5"
        return httpx.Response(200, json={"departures": [
            {"stop_id": 1, "route_id": 9, "run_ref": "run-A", "platform_number": "2",
             "scheduled_departure_utc": "2026-11-17T00:00:00Z",
             "estimated_departure_utc": "2026-11-17T00:02:00Z", "disruption_ids": [101]},
            {"stop_id": 1, "route_id": 9, "run_ref": "run-B", "disruption_ids": []},
            {"stop_id": 1, "route_id": 9, "run_ref": "run-C"},
            {"stop_id": 1, "route_id": None, "run_ref": "run-D", "disruption_ids": [999]},
        ], "disruptions": {
            "101": {"disruption_id": 101, "title": "Planned works", "disruption_status": "Planned"},
            "102": {"disruption_id": 102, "title": "Contextual notice", "disruption_status": "Current"},
        }})

    monkeypatch.setattr(departure_board, "search_stations", lambda name, config: ptv.search_stations(
        name, config, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(departure_board, "get_departures_with_disruptions", lambda stop_id, **kwargs:
                        departure_service.get_departures_with_disruptions(
                            stop_id, **kwargs, transport=httpx.MockTransport(handler)))
    departure_board.main()
    assert paths == ["/v3/search/Richmond", "/v3/departures/route_type/0/stop/1"]
    output = capsys.readouterr().out
    for expected in ("run: run-A | platform: 2", "Scheduled: 2026-11-17 11:00:00 AEDT",
                     "Real-time estimate: 2026-11-17 11:02:00 AEDT", "Real-time estimate: unavailable",
                     "Directly associated notices (matched by disruption ID)", "PTV status: Planned",
                     "To: unknown", "PTV listed no directly associated disruption IDs.",
                     "PTV did not supply disruption association IDs.",
                     "Referenced notices with unavailable details: 999",
                     "Other notices in the departures response (no direct association"):
        assert expected in output
    assert output.count("ID 101:") == 1
    assert output.count("ID 102:") == 1
    assert config.api_key not in output
    assert config.dev_id not in output


@pytest.mark.parametrize("date,expected", [
    (datetime(2026, 7, 14, 23, tzinfo=timezone.utc), "2026-07-15 09:00:00 AEST"),
    (datetime(2026, 1, 14, 23, tzinfo=timezone.utc), "2026-01-15 10:00:00 AEDT"),
    (None, "unavailable"),
])
def test_console_time_conversion(date, expected):
    assert departure_board._time(date, ZoneInfo("Australia/Melbourne")) == expected


@pytest.mark.parametrize("stations", [[], [station(), station("Richmond Station", 2)], [station("East Richmond")]])
def test_no_unique_station_does_not_request_a_board(config, monkeypatch, capsys, stations):
    monkeypatch.setattr(departure_board, "search_stations", lambda *a: stations)
    monkeypatch.setattr(departure_board, "get_departures_with_disruptions",
                        lambda *a, **kw: pytest.fail("No selected station"))
    departure_board.main()
    assert capsys.readouterr().out


def test_api_failure_is_displayed_safely(config, monkeypatch, capsys):
    monkeypatch.setattr(departure_board, "search_stations", lambda *a: [station()])

    def fail(*args, **kwargs):
        raise ptv.PTVRequestError("PTV request failed (HTTP 403)")

    monkeypatch.setattr(departure_board, "get_departures_with_disruptions", fail)
    with pytest.raises(SystemExit, match="Request failed: PTV request failed"):
        departure_board.main()
    assert "no directly associated" not in capsys.readouterr().out.lower()


def test_missing_credentials_before_search(monkeypatch):
    def missing():
        raise ValueError("Missing required environment variables: PTV_API_KEY")

    monkeypatch.setattr(departure_board.PTVConfig, "from_env", missing)
    monkeypatch.setattr(departure_board, "search_stations", lambda *a: pytest.fail("Missing configuration"))
    with pytest.raises(SystemExit, match="PTV_API_KEY"):
        departure_board.main()


def test_missing_timezone_before_search(config, monkeypatch):
    def missing(key):
        raise ZoneInfoNotFoundError(key)

    monkeypatch.setattr(departure_board, "ZoneInfo", missing)
    monkeypatch.setattr(departure_board, "search_stations", lambda *a: pytest.fail("Missing timezone data"))
    with pytest.raises(SystemExit, match="uv sync"):
        departure_board.main()
