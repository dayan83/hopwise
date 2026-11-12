import json

from ptv import PTVRequestError, get_route_types


def main():
    try:
        result = get_route_types()
    except (ValueError, PTVRequestError) as exc:
        raise SystemExit(str(exc)) from None
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
