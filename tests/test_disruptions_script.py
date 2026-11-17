from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import pytest

import disruptions
import ptv


@pytest.fixture
def config(monkeypatch):
    config = ptv.PTVConfig("fake-id", "fake-key", "https://ptv.example")
    monkeypatch.setattr(disruptions.PTVConfig, "from_env", lambda: config)
    return config


def station(name="Richmond", stop_id=1):
    return {"stop_name": name, "stop_suburb": "Richmond", "stop_id": stop_id, "route_type": 0}


def test_richmond_to_route_disruptions_flow(config, monkeypatch, capsys):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        assert request.url.params["devid"] == config.dev_id
        assert "signature" in request.url.params
        if request.url.path == "/v3/search/Richmond":
            return httpx.Response(200, json={"stops": [
                station("East Richmond", 99), station("Richmond Station")]})
        if request.url.path == "/v3/departures/route_type/0/stop/1":
            return httpx.Response(200, json={"departures": [
                {"route_id": 9}, {"route_id": 9}, {"route_id": 10}, {}, {"route_id": True}]})
        if request.url.path == "/v3/disruptions/route/9":
            return httpx.Response(200, json={"disruptions": {
                "metro_train": [
                    {"disruption_id": 101, "title": "Track works", "description": "Altered timetable",
                     "disruption_status": "Planned", "disruption_type": "Service alteration",
                     "from_date": "2026-11-17T00:00:00Z", "to_date": None,
                     "last_updated": "2026-11-16T00:00:00Z",
                     "routes": [{"route_id": 9, "route_name": "Example line"}],
                     "stops": [{"stop_id": 1, "stop_name": "Richmond"}],
                     "url": "https://ptv.example/notices/101"},
                    {"disruption_id": 102, "title": "Delays", "disruption_status": "Current"}],
                "general": [{"disruption_id": 103, "title": "General notice"}]}})
        if request.url.path == "/v3/disruptions/route/10":
            return httpx.Response(200, json={"disruptions": {}})
        pytest.fail(f"Unexpected endpoint: {request.url.path}")

    monkeypatch.setattr(disruptions, "search_stations", lambda name, config: ptv.search_stations(
        name, config, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(disruptions, "get_departures", lambda stop_id, **kwargs: ptv.get_departures(
        stop_id, **kwargs, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(disruptions, "get_disruptions_for_route", lambda route_id, **kwargs:
                        ptv.get_disruptions_for_route(route_id, **kwargs,
                                                     transport=httpx.MockTransport(handler)))

    disruptions.main()
    assert paths == ["/v3/search/Richmond", "/v3/departures/route_type/0/stop/1",
                     "/v3/disruptions/route/9", "/v3/disruptions/route/10"]
    output = capsys.readouterr().out
    for expected in ("PTV status: Planned", "PTV status: Current", "To: unknown",
                     "From: 2026-11-17 11:00:00 AEDT", "Last updated: 2026-11-16 11:00:00 AEDT",
                     "Affected routes: Example line (ID: 9)", "Affected stops: Richmond (ID: 1)",
                     "Source URL: https://ptv.example/notices/101", "Groups: general",
                     "scope may span transport modes", "No disruptions returned.",
                     "Description: not specified", "PTV status: not specified"):
        assert expected in output
    assert "active" not in output.lower()
    assert config.api_key not in output
    assert config.dev_id not in output


@pytest.mark.parametrize("value,expected", [
    (None, "unknown"),
    (datetime(2026, 1, 14, 23, tzinfo=timezone.utc), "2026-01-15 10:00:00 AEDT"),
    (datetime(2026, 7, 14, 23, tzinfo=timezone.utc), "2026-07-15 09:00:00 AEST"),
    (datetime(2026, 4, 4, 16, tzinfo=timezone.utc), "2026-04-05 02:00:00 AEST"),
    (datetime(2026, 10, 3, 16, tzinfo=timezone.utc), "2026-10-04 03:00:00 AEDT"),
])
def test_aware_dates_display_in_melbourne(value, expected):
    assert disruptions.format_disruption_time(value, ZoneInfo("Australia/Melbourne")) == expected


@pytest.mark.parametrize("stations", [[], [station(), station("RICHMOND Station", 2)],
                                       [station("East Richmond", 99)]])
def test_no_unique_station_skips_following_requests(config, monkeypatch, capsys, stations):
    monkeypatch.setattr(disruptions, "search_stations", lambda *a: stations)
    monkeypatch.setattr(disruptions, "get_departures", lambda *a, **kw: pytest.fail("No selected station"))
    monkeypatch.setattr(disruptions, "get_disruptions_for_route", lambda *a, **kw: pytest.fail("No route"))
    disruptions.main()
    assert capsys.readouterr().out


@pytest.mark.parametrize("departures", [[], [{"route_id": None}], [{"route_id": True}],
                                         [{"route_id": "9"}], [{"route_id": -1}]])
def test_no_usable_route_ids(config, monkeypatch, capsys, departures):
    monkeypatch.setattr(disruptions, "search_stations", lambda *a: [station()])
    monkeypatch.setattr(disruptions, "get_departures", lambda *a, **kw: departures)
    monkeypatch.setattr(disruptions, "get_disruptions_for_route", lambda *a, **kw: pytest.fail("No route"))
    disruptions.main()
    assert "No usable route IDs" in capsys.readouterr().out


def test_route_error_continues_other_routes(config, monkeypatch, capsys):
    monkeypatch.setattr(disruptions, "search_stations", lambda *a: [station()])
    monkeypatch.setattr(disruptions, "get_departures", lambda *a, **kw: [{"route_id": 9}, {"route_id": 10}])
    called = []

    def request(route_id, **kwargs):
        called.append(route_id)
        if route_id == 9:
            raise ptv.PTVRequestError("PTV request failed (HTTP 403)")
        return []

    monkeypatch.setattr(disruptions, "get_disruptions_for_route", request)
    with pytest.raises(SystemExit) as caught:
        disruptions.main()
    assert caught.value.code == 1
    assert called == [9, 10]
    output = capsys.readouterr().out
    assert "Request failed: PTV request failed (HTTP 403)" in output
    assert "No disruptions returned." in output


@pytest.mark.parametrize("function", ["search_stations", "get_departures"])
def test_initial_request_error(config, monkeypatch, function):
    def fail(*args, **kwargs):
        raise ptv.PTVRequestError("PTV request failed due to a network error")

    monkeypatch.setattr(disruptions, "search_stations", lambda *a: [station()])
    monkeypatch.setattr(disruptions, function, fail)
    monkeypatch.setattr(disruptions, "get_disruptions_for_route", lambda *a, **kw: pytest.fail("No route"))
    with pytest.raises(SystemExit, match="Request failed"):
        disruptions.main()


def test_missing_credentials_before_search(monkeypatch):
    def missing():
        raise ValueError("Missing required environment variables: PTV_API_KEY")

    monkeypatch.setattr(disruptions.PTVConfig, "from_env", missing)
    monkeypatch.setattr(disruptions, "search_stations", lambda *a: pytest.fail("No credentials"))
    with pytest.raises(SystemExit, match="PTV_API_KEY"):
        disruptions.main()


def test_missing_timezone_before_search(config, monkeypatch):
    def missing(key):
        raise ZoneInfoNotFoundError(key)

    monkeypatch.setattr(disruptions, "ZoneInfo", missing)
    monkeypatch.setattr(disruptions, "search_stations", lambda *a: pytest.fail("No timezone data"))
    with pytest.raises(SystemExit, match="uv sync"):
        disruptions.main()
