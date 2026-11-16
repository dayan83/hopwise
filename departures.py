"""Search three Melbourne stations and display their upcoming departures."""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ptv import PTVConfig, PTVRequestError, get_departures, search_stations


def format_departure_time(value: str | None, timezone: ZoneInfo) -> str:
    """Format an aware API timestamp; never assume a timezone for invalid data."""
    if not isinstance(value, str) or not value:
        return "unavailable"
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            return "unavailable"
        return timestamp.astimezone(timezone).strftime("%Y-%m-%d %H:%M:%S %Z")
    except (ValueError, OverflowError):
        return "unavailable"


def _station_name(name: str) -> str:
    return name.strip().casefold().removesuffix(" station").strip()


def _display(value) -> str:
    return "unavailable" if value is None or value == "" else str(value)


def main():
    try:
        config = PTVConfig.from_env()
        melbourne = ZoneInfo("Australia/Melbourne")
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    except ZoneInfoNotFoundError:
        raise SystemExit("Melbourne timezone data is missing; run uv sync to install dependencies") from None

    failed = False
    for name in ("Richmond", "Flinders Street", "Glen Waverley"):
        print(f"\n{name} (Australia/Melbourne):")
        try:
            stations = search_stations(name, config)
            if not stations:
                print("  No metropolitan train stations found.")
                continue
            matches = [station for station in stations
                       if _station_name(station["stop_name"]) == _station_name(name)]
            if len(matches) != 1:
                print("  No unique name match; choose a station before requesting departures:")
                for station in stations:
                    print(f"    {station['stop_name']} | {station['stop_suburb']} | "
                          f"stop ID: {station['stop_id']}")
                continue
            station = matches[0]
            print(f"  {station['stop_name']} | {station['stop_suburb']} | stop ID: {station['stop_id']}")
            departures = get_departures(station["stop_id"], route_type=station["route_type"],
                                        config=config)
        except (ValueError, PTVRequestError) as exc:
            print(f"  Request failed: {exc}")
            failed = True
            continue
        if not departures:
            print("  No upcoming departures returned.")
        for departure in departures:
            print(f"  Route: {_display(departure['route_id'])} | "
                  f"direction: {_display(departure['direction_id'])} | "
                  f"platform: {_display(departure['platform_number'])}")
            print(f"    Scheduled: {format_departure_time(departure['scheduled_departure_utc'], melbourne)}")
            print(f"    Real-time estimate: {format_departure_time(departure['estimated_departure_utc'], melbourne)}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
