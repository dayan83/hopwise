# Architecture

[Back to the project](../README.md) | [Evaluation](evaluation.md)

Hopwise separates transport data, deterministic interpretation and presentation.
The Streamlit interface and agent reuse the same PTV integration; neither has its
own authentication, departure matching or HTTP implementation.

## Component responsibilities

| Component | Responsibility |
| --- | --- |
| [ptv.py](../ptv.py) | Load configuration, sign requests, call PTV and parse responses |
| [models.py](../models.py) | Validate structured records and normalize aware timestamps to UTC |
| [departure_service.py](../departure_service.py) | Match notices, share fallback lookups and interpret timing/relevance |
| [departure_presentation.py](../departure_presentation.py) | Format passenger wording from already calculated delay seconds |
| [stopping_pattern_service.py](../stopping_pattern_service.py) | Verify destination stops and order for a run; service layer only, not connected to agent/UI |
| [destination_service.py](../destination_service.py) | Bound candidate/pattern lookups, exclude past departures and return confirmed destination matches with unknown outcomes |
| [agent_tools.py](../agent_tools.py) | Validate tool arguments and serialize service results for the LLM |
| [agent.py](../agent.py) | Execute the bounded Responses API tool-calling loop |
| [agent_trace.py](../agent_trace.py) | Capture optional sanitized execution snapshots |
| [conversation.py](../conversation.py) | Store session preferences without caching transport records |
| [streamlit_app.py](../streamlit_app.py) | Render station search, departure boards, notices and chat |
| [ask_hopwise.py](../ask_hopwise.py) | Configure one terminal question and optionally print its trace |

The flat module layout keeps this small project easy to navigate. Dependencies
are injected at the client and HTTP transport boundaries so tests can exercise
the application without external APIs.

## A question from request to answer

For **"What are the next trains from Richmond, and are there any disruptions?"**:

1. The UI or CLI calls `answer_question_from_env()`. It reads local configuration
   and opens the OpenAI client; `answer_question()` manages the execution.
2. `client.responses.create()` receives the question, instructions and two strict
   function schemas. The model may request `search_stations(query)`.
3. `execute_tool()` validates the request and calls `ptv.search_stations()`. The
   result contains station names, suburbs and stop IDs. An intended station must
   be resolved before departures are requested; ambiguous searches require clarification.
4. A subsequent `get_departures_with_disruptions(stop_id, limit)` call is checked
   against the stop IDs returned during this question. Its adapter delegates to
   the departure service.
5. The service uses the shared PTV client to fetch departures with expanded route,
   run and disruption details, returning a typed `DepartureBoard`.
6. The adapter adds Melbourne local times, deterministic timing descriptions and
   limited disruption-relevance annotations. Original UTC values remain intact.
7. The loop appends `function_call_output` with the original `call_id` and sends
   the accumulated context back to the model. It continues until a text answer
   is produced, subject to the five-tool-round bound.

Only `search_stations` and `get_departures_with_disruptions` are allowlisted.
Search terms are validated; stop IDs must be positive integers and result limits
must be integers from 1 to 10. The schemas and runtime checks come from the same
Pydantic argument models. Unexpected fields and coerced numeric strings/booleans
are rejected.

Calls execute sequentially. After five tool rounds, a final model request disables
tools; another attempted call is rejected. Protocol/LLM failures raise sanitized
`AgentError` messages. Tool validation and PTV failures return controlled tool
errors, giving the model an opportunity to explain unavailable information.

## Departure and disruption contracts

| Model | Contents |
| --- | --- |
| `Departure` | Stop, route, run and direction identifiers; names; UTC times; platform; disruption IDs |
| `Disruption` | Notice identity, original status/type, description, date bounds and affected entities |
| `DeparturesResponse` | Departures and expanded notices from one PTV response |
| `DepartureWithDisruptions` | A departure, its directly associated notices and unresolved IDs |
| `DepartureBoard` | The selected stop's departures plus separate contextual/route notices |

`get_departures_response()` requests expansion values `5`, `2` and `3` to reuse
disruption, route and run details. A notice becomes directly associated only when
its ID appears in the departure's `disruption_ids`. Matching the route or a date
window alone does not create an association.

If referenced details are missing, the service requests each necessary route at
most once per service call and shares the results across departures. Missing
details remain in `unresolved_disruption_ids`. Unreferenced expanded notices stay
in `contextual_notices`; unreferenced fallback notices stay in `route_notices`.
No extra route calls are made merely to collect broader context.

`disruption_ids=None` means association data was omitted; `[]` means no IDs were
listed. Empty data does not become a network-wide all-clear. Failed API requests
remain errors rather than being converted to successful empty results.

## Stopping patterns and destination evidence

This increment adds a Python service foundation; destination-specific questions
are not yet exposed through the agent's tools or Streamlit.

`ptv.get_stopping_pattern(run_ref, route_type=0, date_utc=...)` calls
`GET /v3/pattern/run/{run_ref}/route_type/{route_type}` using the existing signing,
HTTP transport and safe error handling. `run_ref` is the string preserved on each
`Departure`, not the deprecated numeric `run_id`. The request expands stops and
runs, includes skipped stops, and does not filter by `stop_id` or request geometry
or interchange data. This keeps the evidence scoped to the selected run.

The [official PTV documentation](https://timetableapi.ptv.vic.gov.au/swagger/ui/index)
defines `departure_sequence` as chronological order, with potentially
non-consecutive values. `PatternDeparture` extends the existing departure model
with that sequence, flags and ordered `skipped_stops`. `PatternStop` stores stop
IDs/names and optional physical `stop_sequence`; physical stop order is not used
as a replacement for chronological departure order. `PatternRun` retains identity
and final-stop metadata. `StoppingPattern` preserves optional/missing data and
the requested scope. All parsed times remain aware UTC values.

`stopping_pattern_service.verify_destination(pattern, boarding_stop_id,
destination_stop_id, departure=...)` is pure deterministic logic:

1. Validate run/route identity and unambiguous stop and departure sequences.
2. Sort by ascending `departure_sequence`, never by response order or by counting
   adjacent sequence numbers. Locate the boarding stop by ID.
3. When supplied, match the source departure's scheduled timestamp, run, stop,
   route and direction. A different trip date or identity remains unknown.
4. Confirm `served` only for an actual departure record at the destination with
   a strictly greater sequence. Stop metadata, route names, final-destination
   names and skipped-stop entries alone never establish service.
5. Before concluding `not_served`, require a matching expanded run whose stated
   final stop is the last departure, and expanded stop IDs fully accounted for
   by departures or explicit skips. Distinguish an earlier/same stop, a skipped
   destination and an absent destination using reason codes.

`get_destination_verification(departure, destination_stop_id, ...)` combines one
pattern request with this check, using the source scheduled time as `date_utc`.
It returns a typed `DestinationVerification` with status, reason, run/stop IDs and
available sequence evidence. PTV failures or invalid response bodies become
`unknown: pattern_unavailable`; empty patterns are also unknown. Invalid caller
destination IDs raise `ValueError` before any request. The lower-level client
continues to raise credential-safe `PTVRequestError` for HTTP/parsing failures.

Missing stop expansion or endpoint metadata can prevent a negative conclusion
while leaving explicit positive departure evidence usable. Missing identities or
sequences, repeated stop visits, tied sequences, inconsistent modes and an offline
API status stay unknown. Contradictory served/skipped occurrences are not resolved
by choosing the first match. Explicit drop-off-only boarding or pick-up-only
destination flags also remain unknown for passenger use. Skipped entries describe
non-stopping locations; they are not evidence that a train has physically passed
them already. No new delay or cancellation claims are inferred.

PTV exposes no completeness flag in this response. Endpoint/coverage checks detect
observable incompleteness, but cannot prove that the upstream feed omitted
nothing. No guarantee of actual train operation, accessibility or full passenger
eligibility is made. This implementation has been validated with synthetic HTTP
fixtures, not a live stopping-pattern contract check.

For the next increment, resolve both station names with the existing search,
retrieve a bounded departure sample and check each distinct run once. Reuse a
parsed pattern for multiple checks within the same request; key any cache by run,
mode and departure date/occurrence, with explicit freshness limits. Five distinct
candidates can add five PTV HTTP calls. Bound the work, preserve unknown results,
and consider bounded concurrency/connection reuse only when integrating the
feature. A small candidate sample cannot prove no suitable service exists later.

Run the focused tests without either API:

```sh
uv run --offline --frozen pytest -q tests/test_stopping_patterns.py
```

### Explicit live stopping-pattern smoke test

To check the real PTV response contract manually, configure the existing PTV
variables in `.env`, then run:

```sh
uv run python smoke_stopping_pattern.py --live
```

The flag is required before credentials are loaded. Importing the script,
running `--help`, or omitting `--live` never sends requests. No OpenAI client is
used. The diagnostic resolves an exact metropolitan Richmond match, requests
at most three departures, selects the first usable upcoming record and fetches
one stopping pattern using its scheduled UTC timestamp. At most three PTV HTTP
requests are made. It does not retry with an altered date filter or select a
different run to hide a pattern mismatch.

Output includes station/run IDs, UTC and Melbourne scheduled times, departure
and expanded-stop counts, first/last stops, and boarding/downstream records in
`departure_sequence` order. Explicit skips are labelled as non-stopping entries.
Missing identifiers, sequences and names, repeated stops, inconsistent identities,
boarding timestamp mismatches and endpoint/coverage discrepancies are reported.
Possible date-filtering or truncation problems remain qualified observations,
not diagnoses of their cause. Malformed responses rejected by the existing client
produce a safe error; raw payloads, signed URLs and credentials are not printed.

The first later listed stop is verified with `verify_destination()` using the
same fetched pattern. A separate negative-coverage control checks the boarding
stop against itself: with unique stops it cannot occur strictly after itself.
Its expected `not_served (destination_not_after_boarding)` result exercises the
existing endpoint/coverage guard without inventing a destination ID. An `unknown`
control means missing destinations must not be treated as confirmed absences.
No matching/completeness rules are reimplemented in the script.

Exit codes are **0** for a verified positive probe, **1** for configuration or
request/parsing failure, and **2** for an inconclusive attempt (ambiguity, no
usable departure, no later stop or an unknown probe). Read coverage warnings
even after exit 0: a positive stop can be established while negative coverage
remains unknown. All conclusions describe returned evidence, not guaranteed
train operation. This command was tested offline during implementation; a real
PTV execution remains a deliberate manual step.

Offline command tests use synthetic HTTP fixtures and block credential loading,
live HTTP transports and OpenAI construction:

```sh
uv run --offline --frozen pytest -q tests/test_smoke_stopping_pattern.py
```

## Destination-aware departure service

This Python service is not yet connected to agent tools or the UI. Its callers
must resolve and disambiguate both stations before supplying positive stop IDs.
The new service does not resolve station names or infer service from route names
or final-destination labels.

```python
from destination_service import get_departures_to_destination

result = get_departures_to_destination(
    origin_stop_id, destination_stop_id, limit=3,
    candidate_limit=20, max_pattern_requests=10,
    config=config,
)
```

`get_departures_with_disruptions()` supplies one bounded typed origin board.
The new service retains its departures, directly associated notices, unresolved
IDs, contextual notices and route notices. Each usable upcoming candidate is
checked with the existing pattern client and `verify_destination()`; deterministic
matching rules are not rewritten. Raw timestamps, platforms and run/route names
remain intact. Candidate timing fields reuse `departure_delay()`, `interpret_delay()`
and `passenger_delay_description()` without changing their calculations or wording.

The return type is `DestinationDepartureBoard`:

| Field | Meaning |
| --- | --- |
| `matches` | Up to the requested number of unique, confirmed, still-upcoming services, ordered by best available departure time |
| `candidates` | All examined records, with source departure/notices, verification status/reason, time source, upcoming status and exclusions |
| `metadata` | Received/examined counts, budgets, pattern attempts/successes/cache hits, unique confirmed counts, unknowns and exclusions |
| `started_at_utc`, `checked_at_utc` | Aware UTC clock snapshots; final eligibility uses the latter |
| `status`, `reason` | `ok`, `partial` or `unavailable`, plus a bounded-search explanation |
| `contextual_notices`, `route_notices` | Existing broader notice categories, not proof of individual-train impact |

### Upcoming time and ordering

A validated real-time estimate takes precedence over the schedule, including an
early estimate. If no estimate exists, use the scheduled time and preserve the
missing estimate as null. If neither time exists, the candidate is unverifiable
and costs no pattern request. Missing run/boarding identity or missing scheduled
time also prevents a pattern lookup from being tied to the selected departure.

For the live-smoke observation, a 17:00 scheduled train can still be considered
upcoming at 17:01:59 only if its available estimate is at or after 17:01:59. The
schedule alone cannot establish why that live record qualified. Without a usable
future estimate, the past schedule is excluded. A past estimate is not replaced
by a later schedule to make the train appear upcoming.

The service accepts a `clock` callable returning an aware datetime; tests inject
fixed values. It reads the clock before retrieval and again before returning,
normalizes both to UTC and rejects backward/naive clock values. Any candidate
whose best time is strictly before the final snapshot is excluded, even if pattern
retrieval confirmed its stopping pattern. Equality is eligible, not a guarantee
that boarding is possible. No actual train position is inferred. Matches are
chronological among confirmed services in the inspected window; unknown earlier
candidates may also serve the destination.

### Bounds, cache and failure semantics

Default limits are 3 matches, 20 candidate records and 10 pattern attempts. Hard
input caps are 10 matches, 50 candidates and 20 pattern attempts; zero pattern
attempts is allowed and yields explicit unknowns. Requested matches may exceed
the candidate window. Inputs are checked before credentials or HTTP are used.
There is no pagination, repeated origin lookup or automatic retry to fill a count.

Patterns are cached only inside the request using `(run_ref, route_type,
scheduled_departure_utc)`. The full timestamp includes the service date and
distinguishes reused run references even within a day. Every candidate is still
verified against its own boarding record. Failed attempts are cached too, consume
the budget and never become `not_served`. Cache hits remain usable after the
request budget is exhausted. Identical duplicate departure records do not count
as independent trains; conflicting records for the same origin/run/instant are
unverifiable rather than arbitrarily choosing a platform or estimate.

`candidates_examined` counts all records inspected within the candidate cap,
including past, duplicate and unknown records. `pattern_requests` counts attempted
client lookups, including errors; actual pattern HTTP calls cannot exceed it.
`patterns_fetched` counts successfully parsed responses, even if verification
later finds them incomplete. Confirmed counts omit past/duplicate exclusions;
`unknown_verifications` counts unexcluded unverifiable candidates. Matching
records beyond the requested return count remain inspectable in `candidates`.

`candidate_limit_reached` means the source filled the candidate window, not that
additional trains are known to exist. `pattern_request_limit_reached` records
budget attainment; `pattern_budget_skips` identifies candidates actually blocked.
`search_limit_reached` means the candidate window filled or a pattern lookup was
blocked. Such bounds or any unexcluded unknown make the result `partial`, even
when enough confirmed matches were found.

Empty/short results have explicit reasons: `no_candidates_returned`,
`no_upcoming_candidates`, `no_confirmed_matches_within_search`,
`no_confirmed_matches_unverifiable` or `fewer_confirmed_matches_within_search`.
These reasons describe only the inspected window. An individual pattern failure leaves
other matches available and its candidate unknown. A failure of the existing source
board loader (including a required disruption fallback) returns `unavailable` /
`departure_board_unavailable`, with `candidates_received=null`; it is not a
successful empty timetable. Invalid source timestamps are rejected by the existing
client rather than silently substituting schedule data. No raw API error bodies
or authenticated URLs are copied into results.

The pattern budget is not a budget for all PTV traffic: the source board adds its
departure request and the existing deduplicated route-notice fallbacks, themselves
bounded by the candidate count. Pattern calls are sequential in this increment,
so use conservative bounds and inspect the final timestamp/partial status.

The next agent increment should add a small adapter with current-turn provenance
for both resolved stops, then expose the bounded service and its unknown/limit
metadata. The current single-station conversation/search guard needs an explicit
origin/destination design so a destination search cannot overwrite the origin.
Add fixed tool/evaluation cases and wire the test clock before enabling this in
the agent; the present tools, instructions, UI and historical evaluations are unchanged.

```sh
uv run --offline --frozen pytest -q tests/test_destination_service.py
```

## Timing and relevance

Models reject naive datetimes and retain aware UTC timestamps. Display formatting
uses `zoneinfo` with `Australia/Melbourne`; Windows uses the locked `tzdata`
dependency for the timezone database.

`departure_delay()` calculates **estimated minus scheduled**. `interpret_delay()`
supplies exact diagnostic wording without changing that calculation:

| Difference | Meaning |
| --- | --- |
| Positive | Estimated late relative to the schedule |
| Negative | Estimated early relative to the schedule |
| Zero | Estimate matches schedule; no guarantee of on-time operation |
| Missing estimate or schedule | Unknown comparison, represented as `None`/`null` |

The tool adapter also supplies `departure_time_description`: an available estimate
comes first, the schedule remains labelled reference information, and a missing
estimate is explicitly unavailable. It never fills an estimate from the schedule.
The agent is instructed to use this time description verbatim, preserving timestamp
seconds and the prominence of an available estimate.

### Passenger delay wording

`departure_presentation.passenger_delay_description()` formats the already calculated
signed difference. Both the agent tool output and Streamlit's existing Timing
column use it. It does not change `departure_delay()`, `delay_seconds`,
`delay_status`, exact `delay_description` diagnostics, or original timestamps.
The new tool field is `passenger_delay_description`; traces retain both forms.

| Difference | Passenger wording |
| --- | --- |
| Exactly zero | The estimate matches the schedule; this is not a guarantee of on-time operation. |
| More than zero, less than 60 seconds late | Estimate is close to the schedule; timing may change. |
| Less than 60 seconds early | Slightly early (estimated). |
| At least 60 seconds early/late | About N minute(s) early/late (estimated). |
| Missing comparison | Timing comparison unavailable: a scheduled time or real-time estimate is missing. |

For magnitudes of at least 60 seconds, round to the **nearest whole minute,
with exact half-minutes rounded up in magnitude**: `floor(abs(seconds) / 60 + 0.5)`.
This is deliberately not Python's ties-to-even `round()` behaviour. Thus 60 and
61 seconds become about 1 minute late, 129 become about 2 minutes late, 245 become
about 4 minutes late, and 90 seconds early becomes about 2 minutes early. At the
threshold, 59 seconds late receives the neutral sentence; 60 receives about 1 minute.

Small positive differences still have raw status `late`, never zero or `on time`.
Early estimates remain prominently displayed even when described as slightly early.
Streamlit retains explicit placeholders for unavailable estimates or schedules.
The agent uses `passenger_delay_description` verbatim for ordinary answers and
keeps exact seconds for an explicit diagnostic request. It does not perform the
rounding itself. Final LLM compliance remains an evaluation concern.

Relevance annotations compare complete date bounds with the departure timestamp
and identify explicitly affected boarding/other stop IDs. Missing bounds or
location data remain unknown. These annotations preserve associations and PTV
statuses. They do not infer recurring overnight windows, delays, cancellations
or causes from notice prose.

## UI state, tracing and credentials

Streamlit session state preserves the selected stop, fetched board, successful
refresh time and displayed chat history across reruns. Departures load on station
selection or explicit refresh. A failed refresh retains the earlier board with a
warning. Chat history is for display. The assistant separately owns one
`ConversationState` in `st.session_state`, containing just `station_name` and
`departure_limit`. It never inherits the manual selector's stop ID. Each chat
turn starts with a new tool context and no trusted stop IDs or old tool results.

### Basic conversation continuity

`answer_question()` and `answer_question_from_env()` accept an optional
`conversation=ConversationState()` while continuing to return strings. Callers
reuse their own object across turns. Streamlit creates one per browser session;
the terminal CLI and the existing evaluation runner omit it and keep their
single-turn behaviour and base instructions.

Only station/count preferences enter the next model request as explicitly labelled
data, alongside additional conversation instructions. Previous user/assistant
transcripts, IDs, departures and disruption records are not replayed. Every
departure follow-up must search again and then fetch a new board. Existing strict
schemas, the two-tool allowlist, five-round limit and safe API errors still apply.

In conversation mode, a search authorizes only its sole result or unique exact
station-name match (case-insensitive, ignoring a trailing `Station`). Another stop
from the same search is not authorized. Ambiguous searches stop for a deterministic
clarification, and empty/failed searches clear the active station so a subsequent
follow-up cannot silently revert to the old one. Clarification requires a station
name; ordinal choices such as "the second one" are not stored or resolved.

The last validated departure limit is remembered even if retrieval fails, allowing
a retry with the same station/count. No failed retrieval supplies an old board.
A final response without a verified departure attempt is replaced by a station
clarification when no station is known, or rejected with `fresh_data_required`
when fresh information was required. This prevents unsupported departure answers
from being presented as a successful follow-up.

**New conversation** resets both preference fields and the displayed chat. It
does not reset the separate station/departure view. Preferences are in memory only;
they are neither shared between sessions nor stored in persistent accounts.
This supports basic station/count continuity, not general transcript memory or
journey planning. Natural-language intent/count interpretation and final wording
still depend on the LLM and need real-model evaluation.

Run the offline continuity checks:

```sh
uv run pytest -q tests/test_conversation.py tests/test_streamlit_assistant.py
```

`PTVConfig` reads `.env` through `python-dotenv`; existing environment variables
take precedence. `PTVAuth` signs the exact encoded path/query using HMAC-SHA1.
The developer ID and signature are added to the HTTP request, not to tool results.
The shared request layer suppresses request logging that could expose signed URLs.

Tracing is off by default. `--trace` prints the answer to stdout and an allowlisted,
sanitized `AgentTrace` to stderr. It records call order, validated arguments,
success/error codes and selected structured results. Credentials, authenticated
URLs, SDK diagnostics and raw rejected arguments are excluded or redacted.
The ordinary agent entry point continues to return a string. Responses API calls
use `store=False`; no persistent agent memory is implemented.

## Smaller API examples

These scripts use live PTV configuration, without requiring an LLM:

| Command | Demonstration |
| --- | --- |
| `uv run python main.py` | Route types and authentication |
| `uv run python search_stations.py` | Searches for Richmond, Flinders Street and Glen Waverley |
| `uv run python departures.py` | Station search followed by departure retrieval |
| `uv run python disruptions.py` | Route notices for routes found in a departure sample |
| `uv run python departure_board.py` | Richmond's typed departure board with associated/contextual notices |

For an execution walkthrough without API access:

```sh
uv run pytest -v -s tests/test_agent_trace.py::test_trace_records_search_and_departures
```

The printed trace uses scripted LLM responses and mocked PTV services.
