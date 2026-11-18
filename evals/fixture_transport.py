"""Offline PTV response matching and scoped scenario time for evaluations."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import RLock

import httpx
from pydantic import AwareDatetime, BaseModel, Field, ValidationError

from ptv import PTVConfig


_AUTH_PARAMETERS = frozenset({"devid", "signature"})
_REFERENCE_TIME: ContextVar[datetime | None] = ContextVar("ptv_fixture_reference_time", default=None)
_CLOCK_LOCK = RLock()
_active_clock = None


class FixtureMiss(RuntimeError):
    """A fixture mismatch, deliberately separate from PTV/network failures."""

    code = "fixture_miss"

    def __init__(self):
        # Never include a request, URL, query values or authentication diagnostics.
        super().__init__("fixture_miss: no response matches the request method, path and query parameters.")


class _Request(BaseModel):
    method: str = Field(min_length=1)
    path: str
    query: dict[str, str | list[str]]


class _Response(BaseModel):
    status_code: int = Field(strict=True, ge=100, le=599)
    body: dict


class _Entry(BaseModel):
    request: _Request
    response: _Response


class _Scenario(BaseModel):
    scenario_id: str = Field(min_length=1)
    as_of_utc: AwareDatetime
    responses: list[_Entry] = Field(min_length=1)


def _query_key(query):
    """Decoded names/values; ordering is irrelevant, multiplicity is retained."""
    return tuple(sorted(
        (key, tuple(sorted(values if isinstance(values, list) else [values])))
        for key, values in query.items() if key not in _AUTH_PARAMETERS
    ))


class _ScopedClock:
    def __init__(self, original):
        self.original = original
        self.users = 0

    def now(self, tz=None):
        reference = _REFERENCE_TIME.get()
        if reference is None:
            return self.original.now(tz)
        return reference.astimezone(tz) if tz is not None else reference.astimezone().replace(tzinfo=None)

    def __getattr__(self, name):
        return getattr(self.original, name)

    def __call__(self, *args, **kwargs):
        return self.original(*args, **kwargs)


class FixtureTransport(httpx.MockTransport):
    """Reusable fixture responses, with no real-network transport underneath.

    Repeated identical requests reuse the same response and increment its count.
    A new instance starts a fresh scenario; unused requests are not asserted on
    close because the PTV client opens/closes a client for each service call.
    """

    def __init__(self, fixture: dict):
        try:
            scenario = _Scenario.model_validate(deepcopy(fixture))
            lookup = {}
            for index, entry in enumerate(scenario.responses):
                request = entry.request
                if (not request.path.startswith("/v3/")
                        or "?" in request.path or "#" in request.path
                        or any(value == [] for value in request.query.values())):
                    raise ValueError
                key = (request.method.upper(), httpx.URL(request.path).raw_path, _query_key(request.query))
                if key in lookup:
                    raise ValueError
                lookup[key] = index
        except (ValidationError, ValueError, httpx.InvalidURL):
            raise ValueError("invalid_fixture: check scenario time, response definitions and unique request keys.") from None
        self._scenario = scenario
        self._lookup = lookup
        self._match_counts = [0] * len(scenario.responses)
        self.miss_count = 0
        self.scenario_id = scenario.scenario_id
        self.as_of_utc = scenario.as_of_utc.astimezone(timezone.utc)
        self.config = PTVConfig("fixture-developer", "fixture-key", "https://ptv-fixture.invalid")
        super().__init__(self._respond)

    @classmethod
    def from_file(cls, path: str | Path) -> "FixtureTransport":
        try:
            fixture = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError("invalid_fixture: unable to load fixture JSON.") from None
        return cls(fixture)

    def _respond(self, request: httpx.Request) -> httpx.Response:
        query = {key: request.url.params.get_list(key) for key in request.url.params}
        path = request.url.raw_path.split(b"?", 1)[0]
        key = (request.method.upper(), path, _query_key(query))
        index = self._lookup.get(key)
        if index is None:
            self.miss_count += 1
            raise FixtureMiss()
        self._match_counts[index] += 1
        response = self._scenario.responses[index].response
        return httpx.Response(response.status_code, json=response.body)

    @property
    def match_counts(self) -> tuple[int, ...]:
        return tuple(self._match_counts)

    @property
    def unused_requests(self) -> list[dict]:
        """Descriptions from the fixture, never raw authenticated HTTP requests."""
        return deepcopy([{
            "response_index": index + 1,
            "method": entry.request.method.upper(),
            "path": entry.request.path,
            "query": {key: value for key, value in entry.request.query.items() if key not in _AUTH_PARAMETERS},
        } for index, entry in enumerate(self._scenario.responses) if not self._match_counts[index]])

    def assert_all_used(self):
        unused = [entry["response_index"] for entry in self.unused_requests]
        if unused:
            raise AssertionError(f"fixture_unused: response entries {unused} were never executed.")

    @contextmanager
    def reference_time(self):
        """Freeze only agent tool retrieval time in the current execution context.

        Other threads/contexts still see their original clock. Nested and
        overlapping scopes restore the module attribute after the final exit,
        including exceptions; global datetime and HTTP timeout clocks are untouched.
        """
        import agent_tools

        global _active_clock
        with _CLOCK_LOCK:
            if _active_clock is None:
                _active_clock = _ScopedClock(agent_tools.datetime)
                agent_tools.datetime = _active_clock
            clock = _active_clock
            clock.users += 1
        token = _REFERENCE_TIME.set(self.as_of_utc)
        try:
            yield self.as_of_utc
        finally:
            _REFERENCE_TIME.reset(token)
            with _CLOCK_LOCK:
                clock.users -= 1
                if clock.users == 0:
                    agent_tools.datetime = clock.original
                    _active_clock = None
