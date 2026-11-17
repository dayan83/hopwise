"""Minimal authenticated access to the PTV Timetable API V3."""

import hashlib
import hmac
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from dotenv import load_dotenv


@dataclass(frozen=True)
class PTVConfig:
    dev_id: str = field(repr=False)
    api_key: str = field(repr=False)
    base_url: str

    @classmethod
    def from_env(cls, env_path: str | Path | None = None) -> "PTVConfig":
        load_dotenv(env_path or Path(__file__).with_name(".env"), override=False)
        names = ("PTV_DEV_ID", "PTV_API_KEY", "PTV_BASE_URL")
        values = [os.environ.get(name, "").strip() for name in names]
        missing = [name for name, value in zip(names, values) if not value]
        if missing:
            raise ValueError("Missing required environment variables: " + ", ".join(missing))
        config = cls(*values)
        try:
            url = httpx.URL(config.base_url)
            valid = (url.scheme in ("http", "https") and url.host
                     and url.path == "/" and not url.query
                     and not url.fragment and not url.userinfo)
        except httpx.InvalidURL:
            valid = False
        if not valid:
            raise ValueError("PTV_BASE_URL must be an HTTP(S) origin URL")
        return config


class PTVAuth(httpx.Auth):
    """Sign the exact encoded path and query sent by httpx."""

    def __init__(self, dev_id: str, api_key: str):
        self._dev_id = dev_id
        self._api_key = api_key.encode("utf-8")

    def auth_flow(self, request):
        url = request.url.copy_remove_param("signature").copy_set_param("devid", self._dev_id)
        signature = hmac.new(self._api_key, url.raw_path, hashlib.sha1).hexdigest().upper()
        request.url = url.copy_add_param("signature", signature)
        yield request


class PTVRequestError(RuntimeError):
    """A request failed; messages intentionally omit authenticated URLs."""


def get_route_types(config: PTVConfig | None = None, *,
                    transport: httpx.BaseTransport | None = None) -> dict:
    return _get_json("/v3/route_types", config, transport=transport)


def search_stations(name: str, config: PTVConfig | None = None, *,
                    transport: httpx.BaseTransport | None = None) -> list[dict]:
    """Return all matching metropolitan train stops; an empty list means no match.

    Multiple matches are retained for the caller to disambiguate using name,
    suburb and stop ID. PTV searches names by substring, not exact equality.
    """
    term = name.strip()
    if len(term) < 3 or term.isnumeric():
        raise ValueError("Station name must contain at least three characters and not be numeric")
    payload = _get_json(
        "/v3/search/" + quote(term, safe=""), config,
        params={"route_types": "0", "include_outlets": "false",
                "match_stop_by_suburb": "false", "match_route_by_suburb": "false"},
        transport=transport,
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("stops"), list):
        raise PTVRequestError("PTV returned an invalid station search response")
    stations = []
    for stop in payload["stops"]:
        if not isinstance(stop, dict):
            raise PTVRequestError("PTV returned an invalid station search response")
        if stop.get("route_type") != 0:
            continue
        if (not isinstance(stop.get("stop_name"), str)
                or not isinstance(stop.get("stop_id"), int)):
            raise PTVRequestError("PTV returned an invalid station search response")
        stations.append({"stop_name": stop["stop_name"],
                         "stop_suburb": stop.get("stop_suburb") or "",
                         "stop_id": stop["stop_id"], "route_type": 0})
    return stations


def get_departures(stop_id: int, route_type: int = 0, max_results: int = 5, *,
                   config: PTVConfig | None = None,
                   transport: httpx.BaseTransport | None = None) -> list[dict]:
    """Return departures in PTV response order, retaining UTC times unchanged.

    Missing fields are None, including estimates that PTV does not provide.
    Empty departures return []; malformed response structures raise PTVRequestError.
    """
    for name, value, minimum in (("stop_id", stop_id, 1),
                                 ("route_type", route_type, 0),
                                 ("max_results", max_results, 1)):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    payload = _get_json(
        f"/v3/departures/route_type/{route_type}/stop/{stop_id}", config,
        params={"max_results": str(max_results)}, transport=transport,
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("departures"), list):
        raise PTVRequestError("PTV returned an invalid departures response")
    fields = ("route_id", "direction_id", "scheduled_departure_utc",
              "estimated_departure_utc", "platform_number")
    departures = []
    for departure in payload["departures"][:max_results]:
        if not isinstance(departure, dict):
            raise PTVRequestError("PTV returned an invalid departures response")
        departures.append({field: departure.get(field) for field in fields})
    return departures


def get_disruptions_for_route(route_id: int, *, config: PTVConfig | None = None,
                              transport: httpx.BaseTransport | None = None) -> list[dict]:
    """Return metro_train and general notices from the route-scoped endpoint.

    Preserve PTV statuses without inferring activity. Dates are aware UTC
    datetimes or None. General notices retain their broader grouping context.
    """
    if isinstance(route_id, bool) or not isinstance(route_id, int) or route_id < 0:
        raise ValueError("route_id must be a nonnegative integer")
    payload = _get_json(f"/v3/disruptions/route/{route_id}", config, transport=transport)
    if not isinstance(payload, dict):
        raise PTVRequestError("PTV returned an invalid disruptions response")
    groups = payload.get("disruptions")
    if groups is None:
        return []
    if not isinstance(groups, dict):
        raise PTVRequestError("PTV returned an invalid disruptions response")

    disruptions = []
    by_id = {}
    for group in ("metro_train", "general"):
        records = groups.get(group)
        if records is None:
            continue
        if not isinstance(records, list):
            raise PTVRequestError("PTV returned an invalid disruptions response")
        for record in records:
            disruption = _parse_disruption(record)
            disruption["source_groups"] = [group]
            disruption_id = disruption["disruption_id"]
            if disruption_id is None or disruption_id not in by_id:
                disruptions.append(disruption)
                if disruption_id is not None:
                    by_id[disruption_id] = disruption
                continue
            # Metro records come first. Keep their nonmissing scalar values,
            # while retaining additional scope and affected entities from duplicates.
            existing = by_id[disruption_id]
            if group not in existing["source_groups"]:
                existing["source_groups"].append(group)
            for field, value in disruption.items():
                if existing[field] is None:
                    existing[field] = value
                elif field in ("routes", "stops") and value is not None:
                    for entity in value:
                        if entity not in existing[field]:
                            existing[field].append(entity)
    return disruptions


def _parse_disruption(record) -> dict:
    if not isinstance(record, dict):
        raise PTVRequestError("PTV returned an invalid disruption record")
    disruption_id = record.get("disruption_id")
    if disruption_id is not None and (isinstance(disruption_id, bool)
            or not isinstance(disruption_id, int) or disruption_id < 0):
        raise PTVRequestError("PTV returned an invalid disruption ID")
    result = {"disruption_id": disruption_id}
    for field in ("title", "description", "disruption_status", "disruption_type", "url"):
        value = record.get(field)
        if value is not None and not isinstance(value, str):
            raise PTVRequestError("PTV returned an invalid disruption field")
        result[field] = value
    for field in ("from_date", "to_date", "last_updated"):
        result[field] = _parse_disruption_datetime(record.get(field))
    for field in ("routes", "stops"):
        value = record.get(field)
        if value is not None and (not isinstance(value, list)
                or any(not isinstance(entity, dict) for entity in value)):
            raise PTVRequestError("PTV returned invalid affected routes or stops")
        result[field] = [dict(entity) for entity in value] if value is not None else None
    return result


def _parse_disruption_datetime(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            timestamp = datetime.fromisoformat(value)
            if timestamp.tzinfo is not None:
                return timestamp.astimezone(timezone.utc)
        except (ValueError, OverflowError):
            pass
    # Do not guess a timezone or expose the raw field in an error message.
    raise PTVRequestError("PTV returned an invalid disruption timestamp")


def _get_json(path: str, config: PTVConfig | None = None, *,
              params: dict[str, str] | None = None,
              transport: httpx.BaseTransport | None = None) -> dict:
    config = config or PTVConfig.from_env()
    # httpx logs full URLs at INFO; httpcore debug logs may include request data.
    loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    previous = [logger.disabled for logger in loggers]
    for logger in loggers:
        logger.disabled = True
    # Child httpcore loggers also propagate directly to root handlers.
    core_loggers = [logger for name, logger in logging.Logger.manager.loggerDict.items()
                    if name.startswith("httpcore.") and isinstance(logger, logging.Logger)]
    core_previous = [logger.disabled for logger in core_loggers]
    for logger in core_loggers:
        logger.disabled = True
    try:
        with httpx.Client(base_url=config.base_url,
                          auth=PTVAuth(config.dev_id, config.api_key),
                          timeout=10.0, transport=transport) as client:
            response = client.get(path, params=params)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        message = f"PTV request failed (HTTP {exc.response.status_code})"
    except httpx.HTTPError:
        message = "PTV request failed due to a network error"
    except ValueError:
        message = "PTV returned invalid JSON"
    finally:
        for logger, disabled in zip(loggers, previous):
            logger.disabled = disabled
        for logger, disabled in zip(core_loggers, core_previous):
            logger.disabled = disabled
    # Raise outside the handler so even traceback context contains no signed URL.
    raise PTVRequestError(message)
