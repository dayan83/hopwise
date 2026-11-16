from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import pytest

import departures
import ptv


@pytest.fixture
def config(monkeypatch):
    config = ptv.PTVConfig("fake-id", "fake-key", "https://ptv.example")
    monkeypatch.setattr(departures.PTVConfig, "from_env", lambda: config)
    return config


def station(name, stop_id=123):
    return {"stop_name": name, "stop_suburb": "Melbourne", "stop_id": stop_id, "route_type": 0}


@pytest.mark.parametrize("utc,expected", [
    ("2026-01-14T23:00:00Z", "2026-01-15 10:00:00 AEDT"),
    ("2026-07-14T23:00:00+00:00", "2026-07-15 09:00:00 AEST"),
    ("2026-04-04T15:59:00Z", "2026-04-05 02:59:00 AEDT"),
    ("2026-04-04T16:00:00Z", "2026-04-05 02:00:00 AEST"),
    ("2026-10-03T15:59:00Z", "2026-10-04 01:59:00 AEST"),
    ("2026-10-03T16:00:00Z", "2026-10-04 03:00:00 AEDT"),
])
def test_melbourne_time_conversion(utc, expected):
    assert departures.format_departure_time(utc, ZoneInfo("Australia/Melbourne")) == expected


@pytest.mark.parametrize("value", [None, "", "invalid", "2026-11-14T23:00:00", 123])
def test_missing_or_invalid_times_are_unavailable(value):
    assert departures.format_departure_time(value, ZoneInfo("Australia/Melbourne")) == "unavailable"


def test_search_and_departures_flow(config, monkeypatch, capsys):
    names = {"Richmond": 1, "Flinders Street": 2, "Glen Waverley": 3}
    paths = []

    def handler(request):
        paths.append(request.url.path)
        assert request.url.params["devid"] == config.dev_id
        assert "signature" in request.url.params
        if request.url.path.startswith("/v3/search/"):
            name = request.url.path.removeprefix("/v3/search/")
            stops = [station(name + " Station", names[name])]
            if name == "Richmond":
                stops.insert(0, station("East Richmond", 99))
            return httpx.Response(200, json={"stops": stops})
        stop_id = int(request.url.path.rsplit("/", 1)[1])
        assert stop_id in names.values()
        assert request.url.params["max_results"] == "5"
        departure = {"route_id": 9, "direction_id": 0, "platform_number": "2",
                     "scheduled_departure_utc": "2026-11-14T23:00:00Z"}
        if stop_id == 1:
            departure["estimated_departure_utc"] = "2026-11-14T23:02:00Z"
        elif stop_id == 2:
            departure["estimated_departure_utc"] = None
        return httpx.Response(200, json={"departures": [departure]})

    monkeypatch.setattr(departures, "search_stations", lambda name, config: ptv.search_stations(
        name, config, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(departures, "get_departures", lambda stop_id, **kwargs: ptv.get_departures(
        stop_id, **kwargs, transport=httpx.MockTransport(handler)))

    departures.main()
    output = capsys.readouterr().out
    assert paths == [path for name, stop_id in names.items() for path in (
        f"/v3/search/{name}", f"/v3/departures/route_type/0/stop/{stop_id}")]
    assert output.count("Scheduled: 2026-11-15 10:00:00 AEDT") == 3
    assert output.count("Real-time estimate: unavailable") == 2
    assert "Real-time estimate: 2026-11-15 10:02:00 AEDT" in output
    assert "direction: 0 | platform: 2" in output
    assert config.dev_id not in output
    assert config.api_key not in output


def test_no_stations_never_fetches_departures(config, monkeypatch, capsys):
    monkeypatch.setattr(departures, "search_stations", lambda name, config: [])
    monkeypatch.setattr(departures, "get_departures", lambda *a, **kw: pytest.fail("No station selected"))
    departures.main()
    assert capsys.readouterr().out.count("No metropolitan train stations found.") == 3


@pytest.mark.parametrize("ambiguous", [True, False])
def test_no_unique_name_match_never_fetches(config, monkeypatch, capsys, ambiguous):
    def search(name, config):
        if ambiguous:
            return [station(name, 1), station(name.upper() + " Station", 2)]
        return [station("East " + name, 3)]

    monkeypatch.setattr(departures, "search_stations", search)
    monkeypatch.setattr(departures, "get_departures", lambda *a, **kw: pytest.fail("No station selected"))
    departures.main()
    output = capsys.readouterr().out
    assert output.count("No unique name match") == 3
    assert "stop ID:" in output


def test_empty_departures(config, monkeypatch, capsys):
    monkeypatch.setattr(departures, "search_stations", lambda name, config: [station(name)])
    monkeypatch.setattr(departures, "get_departures", lambda *a, **kw: [])
    departures.main()
    assert capsys.readouterr().out.count("No upcoming departures returned.") == 3


def test_missing_fields_and_zero_direction_display(config, monkeypatch, capsys):
    monkeypatch.setattr(departures, "search_stations", lambda name, config: [station(name)])
    monkeypatch.setattr(departures, "get_departures", lambda *a, **kw: [{
        "route_id": None, "direction_id": 0, "platform_number": None,
        "scheduled_departure_utc": None, "estimated_departure_utc": None,
    }])
    departures.main()
    output = capsys.readouterr().out
    assert "Route: unavailable | direction: 0 | platform: unavailable" in output
    assert "Scheduled: unavailable" in output
    assert "Real-time estimate: unavailable" in output


@pytest.mark.parametrize("failing_function", ["search_stations", "get_departures"])
def test_request_error_continues_other_stations(config, monkeypatch, capsys, failing_function):
    def fail(*args, **kwargs):
        raise ptv.PTVRequestError("PTV request failed (HTTP 403)")

    monkeypatch.setattr(departures, "search_stations", lambda name, config: [station(name)])
    monkeypatch.setattr(departures, "get_departures", lambda *a, **kw: [])
    monkeypatch.setattr(departures, failing_function, fail)
    with pytest.raises(SystemExit) as caught:
        departures.main()
    assert caught.value.code == 1
    output = capsys.readouterr().out
    assert output.count("Request failed: PTV request failed (HTTP 403)") == 3
    assert "Glen Waverley" in output


def test_missing_credentials_exit_before_requests(monkeypatch):
    def missing():
        raise ValueError("Missing required environment variables: PTV_API_KEY")

    monkeypatch.setattr(departures.PTVConfig, "from_env", missing)
    monkeypatch.setattr(departures, "search_stations", lambda *a: pytest.fail("No credentials"))
    with pytest.raises(SystemExit, match="PTV_API_KEY"):
        departures.main()


def test_missing_timezone_data_exits_before_requests(config, monkeypatch):
    def missing(key):
        raise ZoneInfoNotFoundError(key)

    monkeypatch.setattr(departures, "ZoneInfo", missing)
    monkeypatch.setattr(departures, "search_stations", lambda *a: pytest.fail("No timezone data"))
    with pytest.raises(SystemExit, match="uv sync"):
        departures.main()
