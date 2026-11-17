"""Show disruptions for routes found in Richmond's upcoming departures."""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ptv import PTVConfig, PTVRequestError, get_departures, get_disruptions_for_route, search_stations


def format_disruption_time(value: datetime | None, timezone: ZoneInfo) -> str:
    return "unknown" if value is None else value.astimezone(timezone).strftime("%Y-%m-%d %H:%M:%S %Z")


def _display(value) -> str:
    return "not specified" if value is None or value == "" else str(value)


def _affected(entities: list[dict] | None, name_field: str, id_field: str) -> str:
    if not entities:
        return "not specified"
    return "; ".join(f"{_display(entity.get(name_field))} (ID: {_display(entity.get(id_field))})"
                     for entity in entities)


def _station_name(name: str) -> str:
    return name.strip().casefold().removesuffix(" station").strip()


def main():
    try:
        config = PTVConfig.from_env()
        melbourne = ZoneInfo("Australia/Melbourne")
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    except ZoneInfoNotFoundError:
        raise SystemExit("Melbourne timezone data is missing; run uv sync to install dependencies") from None

    try:
        stations = search_stations("Richmond", config)
        if not stations:
            print("No metropolitan train stations found for Richmond.")
            return
        matches = [station for station in stations if _station_name(station["stop_name"]) == "richmond"]
        if len(matches) != 1:
            print("No unique name match; choose a station before requesting disruptions:")
            for station in stations:
                print(f"  {station['stop_name']} | {station['stop_suburb']} | stop ID: {station['stop_id']}")
            return
        station = matches[0]
        departures = get_departures(station["stop_id"], route_type=station["route_type"], config=config)
    except (ValueError, PTVRequestError) as exc:
        raise SystemExit(f"Request failed: {exc}") from None

    route_ids = list(dict.fromkeys(departure["route_id"] for departure in departures
                                  if type(departure["route_id"]) is int and departure["route_id"] >= 0))
    if not route_ids:
        print("No usable route IDs found in Richmond's returned departures.")
        return
    print(f"{station['stop_name']} | stop ID: {station['stop_id']}")
    print("Checking distinct routes from the next five returned departures.")
    failed = False
    for route_id in route_ids:
        print(f"\nRoute {route_id} disruptions (Australia/Melbourne):")
        try:
            notices = get_disruptions_for_route(route_id, config=config)
        except (ValueError, PTVRequestError) as exc:
            print(f"  Request failed: {exc}")
            failed = True
            continue
        if not notices:
            print("  No disruptions returned.")
        for notice in notices:
            print(f"  {_display(notice['title'])} | disruption ID: {_display(notice['disruption_id'])}")
            print(f"    PTV status: {_display(notice['disruption_status'])}")
            print(f"    Type: {_display(notice['disruption_type'])}")
            print(f"    Groups: {', '.join(notice['source_groups'])}")
            if "general" in notice["source_groups"]:
                print("    Includes a general notice returned for this route; scope may span transport modes.")
            print(f"    Description: {_display(notice['description'])}")
            print(f"    From: {format_disruption_time(notice['from_date'], melbourne)}")
            print(f"    To: {format_disruption_time(notice['to_date'], melbourne)}")
            print(f"    Last updated: {format_disruption_time(notice['last_updated'], melbourne)}")
            print(f"    Affected routes: {_affected(notice['routes'], 'route_name', 'route_id')}")
            print(f"    Affected stops: {_affected(notice['stops'], 'stop_name', 'stop_id')}")
            print(f"    Source URL: {_display(notice['url'])}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
