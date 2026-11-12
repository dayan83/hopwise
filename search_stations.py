"""Manually check station search against PTV using the local configuration."""

from ptv import PTVConfig, PTVRequestError, search_stations


def main():
    try:
        config = PTVConfig.from_env()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None

    failed = False
    for name in ("Richmond", "Flinders Street", "Glen Waverley"):
        print(f"\n{name}:")
        try:
            stations = search_stations(name, config)
        except (ValueError, PTVRequestError) as exc:
            print(f"  Search failed: {exc}")
            failed = True
            continue
        if not stations:
            print("  No metropolitan train stations found.")
        elif len(stations) > 1:
            print("  Multiple matches; choose by name, suburb and stop ID:")
        for station in stations:
            print(f"  {station['stop_name']} | {station['stop_suburb']} | "
                  f"stop ID: {station['stop_id']} | route type: {station['route_type']}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
