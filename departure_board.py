"""Display Richmond departures and explicitly associated disruption notices."""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from departure_service import get_departures_with_disruptions
from models import Disruption
from ptv import PTVConfig, PTVRequestError, search_stations


def _time(value: datetime | None, timezone: ZoneInfo) -> str:
    return "unavailable" if value is None else value.astimezone(timezone).strftime("%Y-%m-%d %H:%M:%S %Z")


def _display(value) -> str:
    return "not specified" if value is None or value == "" else str(value)


def _notice(notice: Disruption, timezone: ZoneInfo):
    print(f"    ID {_display(notice.disruption_id)}: {_display(notice.title)}")
    print(f"      PTV status: {_display(notice.disruption_status)} | type: {_display(notice.disruption_type)}")
    print(f"      {_display(notice.description)}")
    end = _time(notice.to_date, timezone) if notice.to_date is not None else "unknown"
    print(f"      From: {_time(notice.from_date, timezone)} | To: {end}")
    print(f"      Last updated: {_time(notice.last_updated, timezone)}")
    print(f"      PTV groups: {', '.join(notice.source_groups) or 'not specified'}")
    print(f"      Source: {_display(notice.url)}")


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
        matches = [station for station in stations
                   if station["stop_name"].strip().casefold().removesuffix(" station").strip() == "richmond"]
        if not stations:
            print("No metropolitan train stations found for Richmond.")
            return
        if len(matches) != 1:
            print("No unique Richmond match; select a station before retrieving departures:")
            for station in stations:
                print(f"  {station['stop_name']} | {station['stop_suburb']} | stop ID: {station['stop_id']}")
            return
        station = matches[0]
        board = get_departures_with_disruptions(station["stop_id"], config=config)
    except (ValueError, PTVRequestError) as exc:
        raise SystemExit(f"Request failed: {exc}") from None

    print(f"{station['stop_name']} | stop ID: {board.stop_id} | Australia/Melbourne")
    if not board.departures:
        print("No upcoming departures returned.")
    for entry in board.departures:
        departure = entry.departure
        print(f"\nRoute: {_display(departure.route_id)} | run: {_display(departure.run_ref)} | "
              f"platform: {_display(departure.platform_number)}")
        print(f"  Scheduled: {_time(departure.scheduled_departure_utc, melbourne)}")
        print(f"  Real-time estimate: {_time(departure.estimated_departure_utc, melbourne)}")
        if departure.disruption_ids is None:
            print("  PTV did not supply disruption association IDs.")
        elif not departure.disruption_ids:
            print("  PTV listed no directly associated disruption IDs.")
        if entry.associated_disruptions:
            print("  Directly associated notices (matched by disruption ID):")
            for notice in entry.associated_disruptions:
                _notice(notice, melbourne)
        if entry.unresolved_disruption_ids:
            print("  Referenced notices with unavailable details: " +
                  ", ".join(map(str, entry.unresolved_disruption_ids)))
    if board.contextual_notices:
        print("\nOther notices in the departures response (no direct association to displayed departures):")
        for notice in board.contextual_notices:
            _notice(notice, melbourne)
    for route_id, notices in board.route_notices.items():
        if notices:
            print(f"\nRoute {route_id} contextual notices (no direct association to displayed departures):")
            for notice in notices:
                _notice(notice, melbourne)


if __name__ == "__main__":
    main()
