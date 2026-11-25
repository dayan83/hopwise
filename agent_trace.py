"""Optional, credential-safe snapshots of agent tool executions."""

from copy import deepcopy
from dataclasses import dataclass, field
import os
import re


# Only these existing tool-result fields can enter a trace. In particular, URLs,
# error messages, SDK objects and configuration objects are never copied.
_RESULT_FIELDS = frozenset("""
    ok error code stations query stop_id stop_name stop_suburb route_type limit
    timezone retrieved_at_utc board departures departure route_id route_name
    run_ref destination_name direction_id scheduled_departure_utc
    estimated_departure_utc platform_number disruption_ids scheduled_local
    estimated_local departure_time_description delay_seconds delay_status delay_description
    passenger_delay_description
    associated_disruptions unresolved_disruption_ids
    contextual_notices route_notices disruption_id title description
    disruption_status disruption_type from_date to_date last_updated
    from_date_local to_date_local last_updated_local routes stops source_groups
    relevance temporal_status temporal_unknown_reason reference_departure_utc
    reference_time_source location_status boarding_stop_id affected_stop_ids
""".split())
_TOOL_NAMES = {"search_stations", "get_departures_with_disruptions"}
_CREDENTIAL_VARIABLES = ("OPENAI_API_KEY", "PTV_API_KEY", "PTV_DEV_ID", "PTV_BASE_URL")
_URL = re.compile(r"(?:https?://|www\.|/v3/)[^\s<>\"']+", re.IGNORECASE)
_CREDENTIAL = re.compile(
    r"\b(?:[\w-]*api[_-]?key|authorization|access[_-]?token|signature|devid|ptv_dev_id)"
    r"\s*[:=]\s*[^\s,;<>]+|\bBearer\s+[^\s,;<>]+|\bsk-[\w-]+",
    re.IGNORECASE,
)


def _text(value: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret.isdecimal():
            # Avoid replacing digits inside unrelated timestamps or identifiers.
            value = re.sub(r"(?<!\w)" + re.escape(secret) + r"(?!\w)", "[REDACTED]", value)
        else:
            value = value.replace(secret, "[REDACTED]")
    return _CREDENTIAL.sub("[REDACTED]", _URL.sub("[URL omitted]", value))


def _redaction_values(secrets):
    return tuple(sorted({
        value for value in (*secrets, *(os.environ.get(key) for key in _CREDENTIAL_VARIABLES))
        if isinstance(value, str) and value
    }, key=len, reverse=True))


def redact_text(value: str, *, secrets=()) -> str:
    """Apply the trace redaction policy to other explicitly selected output text."""
    return _text(value, _redaction_values(secrets))


def _snapshot(value, secrets: tuple[str, ...], *, route_map: bool = False):
    if isinstance(value, dict):
        return {
            key: _snapshot(item, secrets, route_map=key == "route_notices")
            for key, item in value.items()
            if isinstance(key, str) and (
                key.isdecimal() if route_map else key in _RESULT_FIELDS)
        }
    if isinstance(value, list):
        return [_snapshot(item, secrets) for item in value]
    if isinstance(value, str):
        return _text(value, secrets)
    if value is None or type(value) in (bool, int, float):
        return value
    return None


@dataclass
class AgentTrace:
    """One execution's snapshots; never included in the LLM conversation.

    Constructing a collector enables tracing only when it is explicitly passed to
    the agent. Snapshots are independent of the original tool results. No files,
    transcripts, SDK diagnostics or credentials are retained automatically.
    """

    tool_calls: list[dict] = field(default_factory=list)
    status: str = "pending"
    error_code: str | None = None

    def reset(self):
        self.tool_calls.clear()
        self.status = "running"
        self.error_code = None

    def fail(self, code: str):
        self.status = "failed"
        self.error_code = code

    def record_tool(self, name: str, call_id: str, arguments: dict | None,
                    result: dict, *, secrets=()):
        # Collect redaction values at capture time, after any .env loading. They
        # are used transiently and never stored on the trace object.
        secrets = _redaction_values(secrets)
        self.tool_calls.append({
            "sequence": len(self.tool_calls) + 1,
            "tool_name": name if isinstance(name, str) and name in _TOOL_NAMES else "[unknown tool]",
            "call_id": _text(call_id, secrets),
            "arguments": _snapshot(arguments, secrets),
            "ok": result.get("ok") is True,
            "result": _snapshot(result, secrets),
        })

    def as_dict(self) -> dict:
        return deepcopy({"status": self.status, "error_code": self.error_code,
                         "tool_calls": self.tool_calls})
