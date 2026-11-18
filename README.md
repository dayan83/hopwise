# Hopwise

Set `PTV_DEV_ID`, `PTV_API_KEY`, and `PTV_BASE_URL` in the root `.env` file.
Use `https://timetableapi.ptv.vic.gov.au` for the base URL. Existing environment
variables take precedence over `.env`. Never commit credentials; `.env` is ignored.

Run `uv run python main.py` to fetch and print PTV route types.
Run `uv run python -m pytest` to run the mocked tests without contacting PTV.

`ptv.PTVAuth` signs request paths and query strings using HMAC-SHA1.
`ptv.get_route_types()` loads configuration and performs the authenticated request.
Request logging is temporarily suppressed during this synchronous call because
HTTP logs can expose the developer ID and signature. Do not log authenticated
request objects or URLs when using `PTVAuth` directly.

## Station search

Run `uv run python search_stations.py` to search for Richmond, Flinders Street
and Glen Waverley using your local credentials. Run `uv run python -m pytest`
for offline tests using fake credentials and mocked HTTP responses.

`ptv.search_stations(name)` calls the PTV V3 Search API with `route_types=0`
(metropolitan train), disables outlet and suburb matching, and filters returned
stops by route type again. It returns a list of dictionaries containing
`stop_name`, `stop_suburb`, `stop_id`, and `route_type`. An unavailable suburb
is represented by an empty string.

No matches returns `[]`. Multiple matches are all retained, including partial
name matches such as Richmond and East Richmond; callers must select the desired
stop rather than assume the first result is correct. Blank, shorter-than-three
character, and numeric terms raise `ValueError` because PTV does not support
station searches for these inputs. Request failures raise sanitized
`PTVRequestError` messages.

The script handles display and ambiguity messaging. The search function handles
station selection and response fields. Both search and route types use the same
internal HTTP request function, configuration loader and authentication helper.
No other endpoints are called during station search.

Reference: [PTV V3 API documentation](https://timetableapi.ptv.vic.gov.au/swagger/ui/index).

## Train departures

Install the locked dependencies, then run the live station/departures example:

```powershell
uv sync --locked
uv run python departures.py
```

The script searches for Richmond, Flinders Street and Glen Waverley using the
existing station search. It selects a unique matching name (ignoring case and a
trailing "Station"). If no unique name matches, it lists candidates and skips
departure retrieval instead of guessing which stop you meant.

The data flows from the script through `search_stations()` to get a stop ID,
then through `get_departures()` to the shared `_get_json()` request function.
Both endpoints reuse `PTVConfig` and `PTVAuth`. The script loads configuration
once; credentials and signed URLs are never displayed.

Use the client directly with a stop ID obtained from station search:

```python
from ptv import get_departures

departures = get_departures(stop_id, route_type=0, max_results=5)
```

`get_departures()` calls `/v3/departures/route_type/{route_type}/stop/{stop_id}`.
It returns at most `max_results` dictionaries, preserving PTV's response order:

| Field | Meaning |
| --- | --- |
| `route_id` | PTV route identifier |
| `direction_id` | PTV direction identifier, including zero |
| `scheduled_departure_utc` | Scheduled ISO 8601 UTC timestamp |
| `estimated_departure_utc` | Real-time ISO 8601 UTC estimate, when supplied |
| `platform_number` | Platform supplied by PTV |

Missing fields are `None`; no departures returns `[]`. Invalid arguments raise
`ValueError`; malformed responses, HTTP errors and network errors raise sanitized
`PTVRequestError`. `stop_id` and `max_results` must be positive integers;
`route_type` must be a nonnegative integer (default 0 for metropolitan trains).

The client keeps UTC strings unchanged. Only `departures.py` converts timestamps
to `Australia/Melbourne`, using standard-library `zoneinfo` for daylight saving.
Windows installs the `tzdata` dependency because it lacks the IANA timezone
database used by `zoneinfo`. Other platforms use their system timezone database.
Output includes dates and AEST/AEDT labels, with separate scheduled and real-time
estimate lines. Missing or invalid timestamps display as "unavailable"; a
scheduled time is never used to fabricate an estimate.

The script continues with the other stations after a request failure, then exits
with status 1 if any request failed. Empty results are displayed normally.
It displays IDs directly and does not make extra requests for route or direction
names, disruptions, or other data.

Run all tests without contacting PTV or using your real credentials:

```powershell
uv run pytest -q
```

`uv run python -m pytest -q` also works. The pytest configuration in
`pyproject.toml` adds the project root to the import path, so both commands can
import the root-level modules such as `ptv.py` and `departures.py`.

Departure tests cover authenticated requests with mocked HTTP responses, missing
estimates and other fields, empty results, failures, ambiguous station selection,
and Melbourne timezone conversion across both daylight-saving transitions.

## Route disruptions

Run the live example with your existing local configuration:

```powershell
uv run python disruptions.py
```

The script searches for Richmond, selects a unique matching metropolitan train
station using the same name rules as the departures example, and retrieves its
next five returned departures. It obtains route IDs from those departures and
requests disruptions once per distinct route ID. This covers the routes in that
departure sample; it is not an exhaustive list of lines serving the station.
No station or route IDs are hardcoded. Ambiguous stations and missing route IDs
are reported without guessing a route.

The data flow is `search_stations()` -> `get_departures()` ->
`get_disruptions_for_route()`. All three reuse `_get_json()`, `PTVConfig`,
`PTVAuth`, timeouts, and sanitized request errors. No dependencies were added.

Call the client directly with a route ID returned by `get_departures()`:

```python
from ptv import get_disruptions_for_route

notices = get_disruptions_for_route(route_id)
```

The client calls `/v3/disruptions/route/{route_id}` and returns dictionaries with:

| Field | Value |
| --- | --- |
| `disruption_id` | PTV disruption ID, or `None` |
| `title`, `description` | PTV text, or `None` |
| `disruption_status`, `disruption_type` | Original PTV values, or `None` |
| `from_date`, `to_date`, `last_updated` | Timezone-aware UTC `datetime` objects, or `None` |
| `routes`, `stops` | Affected entity dictionaries as supplied by PTV, or `None` when unspecified |
| `url` | PTV's source article URL, or `None` |
| `source_groups` | The contributing response groups: `metro_train` and/or `general` |

Only `metro_train` and `general` groups from the route-scoped response are used.
General notices may span transport modes; they remain labelled as general,
including when explicit affected routes or stops are unspecified. The client
relies on the route-scoped endpoint to determine which notices to return.

Notices with the same nonmissing disruption ID are combined. Metro train
records are processed first: their nonmissing scalar values take precedence,
missing values are filled from duplicates, and affected routes/stops and source
groups are combined without repeating identical entries. Notices without IDs
are retained separately.

The client does not filter or relabel statuses or infer whether a notice is
active. Planned, current, and other notices remain distinguishable by the
original `disruption_status`; dates are not used to change that status.

Valid empty responses return `[]`. Missing/null disruption containers and groups
are treated as empty. Missing scalar fields are `None`; missing or empty date
values are `None`, with unknown end dates displayed as "unknown". Malformed
structures or invalid/timezone-naive timestamps raise sanitized `PTVRequestError`
rather than silently assigning a timezone or reporting no disruptions.
Route IDs must be nonnegative integers; invalid input raises `ValueError`.

Only console display converts UTC dates to `Australia/Melbourne`, using the
existing `zoneinfo`/Windows `tzdata` setup and including AEST/AEDT labels. The
departure client's existing UTC-string return values are unchanged. The script
shows the source URL, original status, affected entities, and group scope.
It continues with other routes after a disruption request failure and exits
with status 1 if any route request failed.

Run the disruption tests or the full offline suite:

```powershell
uv run pytest -q tests/test_disruptions.py tests/test_disruptions_script.py
uv run pytest -q
```

Tests use fake credentials and mocked HTTP responses, covering planned/current
notices, general groups, deduplication, unknown dates, missing fields, errors,
Melbourne time conversion, and the complete search/departure/disruption flow.

## Departures with associated disruptions

Install the updated dependencies and run the integrated Richmond example:

```powershell
uv sync --locked
uv run python departure_board.py
```

`departure_board.py` selects a unique Richmond station using the existing search
function and passes its stop ID to `departure_service.get_departures_with_disruptions()`.
The service returns structured data; the script converts times to Melbourne time
and prints notices, IDs whose details could not be resolved, and separate
contextual notices. It preserves the distinction between scheduled times and
real-time estimates. A missing estimate stays `None` and displays as "unavailable".

The separation of responsibilities is:

| Component | Responsibility |
| --- | --- |
| `models.py` | Pydantic validation and the structured input/output contracts |
| `ptv.py` | Authentication, HTTP requests, response validation and parsing |
| `departure_service.py` | ID-based associations, shared lookups and fallback request deduplication |
| `departure_board.py` | Station selection and console presentation |

The API client exposes `get_departures_response()` for the richer data. It calls
the same departures endpoint with repeated `expand` values: `5` (Disruption),
`2` (Route), and `3` (Run). It preserves expanded disruption details and resolves
route names and run destinations from the same response, without extra requests.
This response shape was verified against PTV's public Swagger schema; expansion
can still be absent, null, or incomplete and is handled accordingly.
The existing `get_departures()` and route-disruption functions keep their current
dictionary interfaces, so existing scripts keep working. Both departure paths
share one request implementation; the service does not call both paths.

The new models are:

| Model | Contents |
| --- | --- |
| `Departure` | `stop_id`, `route_id`, `route_name`, `run_ref`, `destination_name`, `direction_id`, scheduled/estimated UTC times, platform and `disruption_ids` |
| `Disruption` | Existing notice fields, dates, affected routes/stops and PTV source groups |
| `DeparturesResponse` | Typed departures and expanded notices from the same request |
| `DepartureWithDisruptions` | One departure, its ID-matched notices and unresolved IDs |
| `DepartureBoard` | Selected stop ID, enriched departures and separate contextual/route-level notices |

All timestamps in these models are timezone-aware and normalized to UTC. Naive
timestamps are rejected rather than assigned the machine's timezone. Optional
or empty timestamps become `None`. Disruption statuses are preserved exactly.
Pydantic is used for these new data contracts; the existing configuration
dataclass and scripts have not been migrated.

The service first indexes expanded notices by `disruption_id`. It links a notice
to a departure only when that ID appears in the departure's `disruption_ids`.
A shared route, overlapping date window, title, or PTV status does not create an
association. A PTV `general` notice can still be directly associated when its ID
is explicitly referenced; group scope and departure association are separate.

If referenced IDs are missing from expansion, the service uses the existing
route-disruption function, requesting each necessary route at most once per
service call. Retrieved records are shared across all departures, including
different routes referring to the same disruption. Resolved IDs do not trigger
further lookups. There is no persistent cache.

`unresolved_disruption_ids` retains referenced IDs whose details remain missing
after successful lookups, including departures without a usable route ID.
Missing `disruption_ids` (`None`) means PTV did not supply association information;
an empty list means it listed no IDs. Neither is a claim that the service is
unaffected. Failed API calls remain sanitized errors, not empty notice lists.

Unreferenced expanded records appear in `contextual_notices`. Unreferenced records
from necessary route lookups appear in `route_notices`, keyed by the queried route
ID. Notices explicitly referenced by any displayed departure are excluded from
these contextual lists to avoid presenting a direct association as merely general.
No extra route calls are made solely to populate contextual information. Thus a
board with no contextual notices is not an exhaustive disruption report.

Example use with a stop ID returned by station search:

```python
from departure_service import get_departures_with_disruptions

board = get_departures_with_disruptions(stop_id, max_results=5)
for entry in board.departures:
    print(entry.departure.run_ref, entry.departure.scheduled_departure_utc)
    print([notice.title for notice in entry.associated_disruptions])
    print(entry.unresolved_disruption_ids)
```

Association indicates an API-provided relationship. The service does not infer
cancellation, a delay duration, or any other specific impact from that relationship.

Run the full offline suite:

```powershell
uv run pytest -q
```

The new tests cover expanded-detail reuse, shared disruption IDs, route lookup
deduplication, unlinked route notices, unresolved details, missing association
data/estimates, timezone-aware models, and API failures. The original tests
continue to run unchanged.

## Streamlit station search and departures

Install the updated dependencies, then start the local interface:

```powershell
uv sync --locked
uv run streamlit run streamlit_app.py
```

Open the local URL printed by Streamlit (normally `http://localhost:8501`).
The app uses the same root `.env` configuration as the console scripts. Enter
a station name, click Search, and choose a match by station name, suburb and
stop ID. An empty selector requires an explicit choice, even for one match.
Selecting a station loads its next five returned departures. The table shows
scheduled and estimated times, route, destination, platform and estimate-based
timing. Click **Refresh departures** to request the latest data for that station.

`streamlit_app.py` contains the presentation and widget state. On form submission,
it calls the existing `ptv.search_stations()` function. That function loads
configuration, signs the request and filters metropolitan train stops as before;
the UI contains no API or authentication implementation. Blank input produces
a prompt; unsupported short/numeric names and missing configuration use the
existing client's validation messages. No matches and request failures have
separate messages, and neither leaves an old station selected.

Streamlit reruns the script after widget interactions. `st.session_state` keeps
the search results and selected stop ID for that browser session across reruns:

- `station_results`: the last successful search's station dictionaries.
- `selected_stop_id`: the selected integer stop ID, or `None` before selection.
- `station_choice`: the dropdown's widget value, kept separate from the stored ID.
- `search_complete`: distinguishes a successful empty search from an initial page or error.

Each submitted search clears the earlier results and selection. Choosing a
station updates `selected_stop_id` and loads departures without repeating the
station search. The state is
temporary and session-specific; it is not saved to disk or shared between users,
and a browser reload starts a new session. Credentials stay in the existing
server-side configuration and are not stored in UI state.

Run the UI tests or the complete offline suite:

```powershell
uv run pytest -q tests/test_streamlit_app.py
uv run pytest -q
```

The UI tests use Streamlit's `AppTest` with mocked search results/HTTP responses.
They cover selection persistence, separate sessions, clearing stale results,
empty searches, no matches, validation/configuration errors and API failures.

The departure display follows this flow:

```text
Selected stop ID -> get_departures_with_disruptions() -> existing PTV client
                -> typed DepartureBoard -> Melbourne-time table and notice expanders
```

The client resolves `route_name` from expanded routes using `route_id`, and
`destination_name` from expanded runs using `run_ref`. Missing names remain `None`;
the UI shows a route ID where available and "Not provided" for missing destination
or platform data. It does not guess a destination from a route or direction name.

Pydantic validates timestamp inputs and normalizes them to aware UTC datetimes.
The UI converts these values to `Australia/Melbourne` only when formatting them,
including dates and AEST/AEDT labels. It does not change the stored UTC values.

`departure_service.departure_delay()` returns the signed difference between a
valid estimate and scheduled time. It returns `None` if either is missing.
The UI formats that comparison as late, early or "On time (estimated)". Exact
equality is required for on-time wording. Short differences are displayed in
seconds so a small delay is not rounded into "on time". A missing estimate is
shown explicitly as "No estimate available", never as zero delay. These values
describe PTV's estimate relative to the schedule, not a confirmed impact or cause.

The latest typed board, its selected stop ID, the successful-refresh timestamp
and any retrieval error are held in session state. Departures load once when a
station is selected and again on an explicit refresh; ordinary reruns reuse the
stored response. No automatic polling or global cache is used. The "Last
refreshed" timestamp records successful retrieval completion and is shown in
Melbourne local time, including for valid empty responses.

A new search or station selection clears the previous station's departure data.
If a refresh fails for the same station, the earlier result and its timestamp
remain visible with an error and a warning. An initial failure can be retried
using Refresh departures without automatically retrying on every rerun.

API/authentication and response parsing stay in `ptv.py`; timestamp validation
stays in `models.py`; delay calculation stays in `departure_service.py`; widgets,
refresh state and display formatting stay in `streamlit_app.py`. The UI delegates
notice association and necessary route-disruption fallbacks to the existing
`get_departures_with_disruptions()` service.

The table's Notices column references the service's `associated_disruptions`
for each departure. Full notice descriptions appear in collapsed expanders below
the table, once per disruption ID even when several departures share a notice.
PTV status, type, local dates and source information are kept with the notice.
Missing descriptions and unknown end dates have explicit placeholders.

`unresolved_disruption_ids` appears as "Details unavailable for IDs ..." in the
departure row. Missing association data remains distinct from an explicitly
empty ID list; neither is presented as a guarantee that the train is unaffected.
The UI does not match IDs, infer associations from routes or dates, or change
the Day 5 matching algorithm. It only formats the relationships already returned
by the service and deduplicates their display.

Unlinked notices from `contextual_notices` and `route_notices` appear separately
under Other route/station notices, with the response/route context labelled.
Repeated contextual IDs share one expander that lists all returned contexts.
Notices without IDs remain separate because their identity cannot be established.
A notice's PTV `general` group does not override an explicit departure association.

Association alone is not proof of delay or its cause. The Timing column continues
to depend solely on the estimate/schedule comparison, regardless of linked
notices. Expanding a description does not fetch data again. Refresh updates the
whole departure/notice board together; failed refreshes retain the previous board
and successful-refresh timestamp with a warning.

Run the relevant tests or the full offline suite:

```powershell
uv run pytest -q tests/test_departure_delay.py tests/test_departures_response.py tests/test_streamlit_app.py tests/test_streamlit_departures.py
uv run pytest -q tests/test_streamlit_disruptions.py
uv run pytest -q
```

Tests cover route/run expansion, missing estimates versus on-time estimates,
early/late departures, daylight saving, refreshing, failed refreshes, switching
stations, empty results, and the client/model/UI flow using mocked responses.
Disruption UI tests also cover shared notices, unresolved IDs, missing fields,
planned statuses, contextual notice deduplication, refreshes and reuse of the
real Day 5 service with mocked HTTP responses and a single route fallback.

## First tool-calling agent

The agent uses the official OpenAI Python SDK and Responses API. Its orchestration
is separate from Streamlit and reuses the existing station search and deterministic
departure/disruption service. There are exactly two tools:

| Tool | Existing function |
| --- | --- |
| `search_stations(query)` | `ptv.search_stations(name)` |
| `get_departures_with_disruptions(stop_id, limit)` | `departure_service.get_departures_with_disruptions(stop_id, max_results)` |

Install the locked dependencies and add `OPENAI_API_KEY` and `OPENAI_MODEL` to
your existing root `.env`. Use `.env.example` as a reference. Keep your existing
PTV credentials in place. `OPENAI_MODEL` must name a model available to your API
project that supports Responses function calling; no model is silently selected
for you. Existing environment variables take precedence over `.env` values.

```powershell
uv sync --locked
uv run python ask_hopwise.py "What are the next trains from Richmond, and are there any disruptions?"
```

Running `uv run python ask_hopwise.py` without a question uses that example.
The command makes live OpenAI and PTV requests when configured. Model names,
API access and credentials must be supplied by your own API project.

The new components are:

| File | Responsibility |
| --- | --- |
| `agent_tools.py` | Strict argument models, two function schemas, fixed allowlist and adapters to the existing services |
| `agent.py` | Instructions, bounded tool-calling loop and shared environment-configured entry point |
| `agent_trace.py` | Optional sanitized snapshots of tool arguments, results and execution order |
| `ask_hopwise.py` | Terminal arguments and output using the shared agent entry point |

`answer_question()` sends the question, instructions and JSON tool definitions
through `client.responses.create()`. For each `function_call` it validates the
arguments, executes the allowlisted adapter and appends a `function_call_output`
with the same `call_id`. It sends the accumulated transcript back to the model
until a text answer is returned. Every response output item is retained,
including reasoning items used by reasoning models.

Parallel tool calls are disabled. A question may execute at most five sequential
tool-call rounds. After round five, one final model request disables further
tool selection; a model that still returns a call is rejected before execution.
Invalid or unknown tool calls receive structured error results and consume a
round. Incomplete model responses and OpenAI failures produce safe terminal
errors. The CLI uses a 30-second SDK timeout without automatic request retries.

The function schemas are generated from the same Pydantic models used for Python
validation. Unexpected fields and coerced numeric strings/booleans are rejected.
Search names must contain 3–100 characters and cannot be numeric. Stop IDs must
be positive integers returned by a successful station search within the current
question. Limits must be integers from 1 to 10; the model is instructed to use
five unless the user requests another count. No model-supplied name is evaluated
as Python or looked up dynamically in application globals.

Tool results serialize the existing `DepartureBoard` and preserve associated
notices, contextual notices, unresolved IDs and missing estimates. The adapters
add Python-formatted Melbourne local times and the existing `departure_delay()`
comparison, leaving the original UTC fields intact. `interpret_delay()` in
`departure_service.py` adds deterministic `delay_status` and `delay_description`
fields to each departure in the tool output:

| `delay_seconds` | `delay_status` | `delay_description` example |
| --- | --- | --- |
| `30` | `late` | Estimated 30 seconds late relative to the schedule. |
| `-30` | `early` | Estimated 30 seconds early relative to the schedule. |
| `0` | `matches_schedule` | The estimate matches the schedule; this is not a guarantee of on-time operation. |
| `null` | `unknown` | Timing comparison unavailable: a scheduled time or real-time estimate is missing. |

The sign is always estimated minus scheduled. A missing estimate or schedule
leaves `delay_seconds` null. The original `Departure` models and their timestamps
are not modified when these derived tool fields are added.

Agent instructions require using `delay_description` verbatim, respecting
`delay_status`, and avoiding independent delay calculations from timestamps.
The model is also instructed to preserve disruption statuses, distinguish missing
information, ask about ambiguous stations and avoid inventing delays or cancellations.
Notice associations are not proof of a delay or its cause, and the returned
departure sample is not an exhaustive network disruption report.

Python supplies the interpretation so the LLM need not infer the sign or duration.
The final prose is still generated by an LLM: it can ignore instructions, mix
departures or omit qualifications, and PTV estimates may change after retrieval.
These fields reduce interpretation errors without validating every final claim.

Credentials remain server-side and are never included in tool definitions or
results. Raw backend/SDK exception messages are not returned to the model. The
agent writes no transcripts or credential logs. Responses use `store=False`;
the transcript and searched stop IDs exist only for the current invocation, with
no conversation memory between questions. The existing PTV/matching logic is
unchanged.

Run the agent tests or full offline suite:

```powershell
uv run pytest -q tests/test_departure_delay.py tests/test_agent_tools.py tests/test_agent.py tests/test_ask_hopwise.py
uv run pytest -q
```

Tests script fake model responses and mock the PTV HTTP boundary. They verify
schema validation, identifier provenance, service reuse, `call_id` correlation,
history preservation, unavailable data, loop limits and credential-safe errors.
An additional fake-HTTP test exercises serialization through the installed
OpenAI SDK without calling OpenAI. These tests validate the implementation;
they do not establish a real model's answer quality or station-selection accuracy.

## Conservative disruption relevance

The agent tool adds a `relevance` object to every serialized disruption notice.
`departure_service.assess_disruption_relevance()` supplies the deterministic
assessment during `_notice_output()` serialization. PTV retrieval, the original
models, disruption-ID matching and all four existing notice/ID categories are
unchanged. No notices are filtered out or promoted into a different category.

| Annotation | Values and meaning |
| --- | --- |
| `temporal_status` | `before_window`, `within_window`, `after_window` or `unknown` |
| `temporal_unknown_reason` | `missing_date_bounds`, `invalid_date_bounds` or `no_departure_time`; otherwise `null` |
| `reference_departure_utc` | The timezone-aware departure instant used, serialized as ISO 8601, or `null` |
| `reference_time_source` | `estimated`, falling back to `scheduled`, or `null` |
| `location_status` | `boarding_station_listed`, `other_stops_listed` or `unknown` |
| `boarding_stop_id` | The selected boarding station's ID |
| `affected_stop_ids` | Deduplicated positive integer stop IDs explicitly supplied by PTV |

Associated notices use their own departure's estimated time when available,
otherwise its scheduled time. Both date bounds are required, and comparisons
include their endpoints. Missing either bound, reversed bounds or a missing
reference time leaves the temporal assessment unknown. A shared notice can have
different time assessments for different departures. UTC-aware comparison also
avoids confusing local daylight-saving transitions.

Board-level `contextual_notices` and `route_notices` have no individual departure
association. Their time assessment remains unknown with `no_departure_time`;
the code does not arbitrarily use the first departure. Their explicit stop IDs
can still be assessed against the selected boarding station. Missing/empty stop
lists or records without valid stop IDs yield unknown location. Names and
descriptions are not used to guess locations or coerce malformed identifiers.
`other_stops_listed` reports the valid IDs present; it does not assert that the
affected-stop list is exhaustive.

These are limited scope annotations, not an overall relevant/irrelevant verdict.
`within_window` only establishes inclusion within the supplied date envelope;
it does not establish recurring operating hours or immediate passenger impact.
No recurring overnight windows are inferred from free-text descriptions. Other
affected stations may still matter to a linked service, and an unknown location
or date must not be treated as evidence of no impact. Original PTV statuses are
preserved; no assessment establishes a delay, cancellation or its cause.

Agent instructions prioritise departure timing and directly associated service
information, explain the annotations' uncertainty and keep broader contextual
notices separate. All annotations also appear in optional execution traces.
The LLM still generates the final prose and can misstate scope or ignore these
instructions. This increment does not add destination/path analysis, recurring
schedule parsing or LLM-based disruption classification.

Run the focused offline tests:

```powershell
uv run pytest -q tests/test_disruption_relevance.py tests/test_agent_trace.py
```

They cover date boundaries, unknown/invalid dates, estimate/schedule selection,
explicit boarding/other-stop IDs, missing stop data, shared notices with different
departure times, preserved associations/categories and unchanged route-request
deduplication. HTTP and model responses are mocked; no live APIs are called.

## Golden evaluation dataset

`evals/dataset.json` version `0.2.0` contains nine synthetic scenarios. The original
five case definitions and their PTV fixtures are unchanged; the reviewed five-case
baseline remains a historical result, not a measurement of these new cases.

| New case | Expected tool calls and response |
| --- | --- |
| `ambiguous_rich_station` | Search `Rich`; present Richmond, East Richmond and West Richmond and ask for clarification. Do not request departures or choose a stop. |
| `richmond_next_three` | Search `Richmond`, then departures for stop `1101` with exactly `limit=3`; report the three returned services. |
| `richmond_30_seconds_early` | Search `Richmond`, then departures for stop `1101`, `limit=5`; scheduled 09:58:00 and estimated 09:57:30 AEDT mean **estimated 30 seconds early**. |
| `glen_waverley_no_departures` | Search `Glen Waverley`, then departures for stop `1103`, `limit=5`; report an empty successful response without inventing services or a cause. |

Each case supplies a fixed `as_of_utc`, fixture path, expected calls/arguments,
independently authored facts, required response notes, prohibited claims and
verification notes. The new fixtures use the existing PTV search and expanded
departure response shapes. Their IDs link stops, routes and runs consistently.
The early estimate is `-30` seconds by direct timestamp subtraction; adding eleven
hours converts the UTC timestamps to Melbourne AEDT. The empty board's reference
time is 02:30 AEDT on 19 November; it establishes no cause or later availability.

The ambiguity fixture supplies search responses only. Any attempted departure
lookup fails with `fixture_miss`. Asking for clarification and avoiding invented
claims still require review of the final answer; offline fixture validation does
not demonstrate how a real LLM will respond. No real LLM evaluations were run as
part of this dataset expansion, and the scoring logic is unchanged.

## Offline PTV fixture transport

`evals/fixture_transport.py` loads the existing synthetic scenario JSON files into
a reusable `FixtureTransport`, a subclass of `httpx.MockTransport`. Pass it through
the existing `transport` argument, along with its dummy `config`. Neither real PTV
credentials nor a real network connection are needed.

```python
from evals.fixture_transport import FixtureTransport
from ptv import search_stations
from departure_service import get_departures_with_disruptions

fixture = FixtureTransport.from_file("evals/fixtures/richmond_departures.json")
stations = search_stations("Richmond", fixture.config, transport=fixture)
board = get_departures_with_disruptions(
    stations[0]["stop_id"], config=fixture.config, transport=fixture)
fixture.assert_all_used()
```

The production client builds and signs each request normally using dummy
credentials. HTTPX sends it to the mock transport's handler in memory, where a
fixture response is returned. The existing JSON parsing, Pydantic models, delay
calculation and disruption matching still execute. Mocking an entire service
function would skip the client and service behaviour beneath that function.

Matching includes the uppercase HTTP method, encoded endpoint path and decoded
application query names/values. Parameter order and repeated expansion-value
order do not matter. Repeated-value multiplicity is preserved. Changed limits,
missing expansion values and unexpected application parameters cause a mismatch.
Only PTV's `devid` and `signature` parameters are ignored. Duplicate fixture
definitions for the same semantic request are rejected during loading.

An unmatched request raises `FixtureMiss`, whose `code` is `fixture_miss`. This
is separate from `PTVRequestError` and HTTPX network errors, so existing service
handling does not turn it into an ordinary "PTV unavailable" result. There is no
live-network fallback. The exception contains no request URL, query values or
credentials, and the transport does not log or retain raw HTTP requests.

`match_counts` reports one count per fixture response definition, and
`unused_requests` describes definitions never matched, using one-based
`response_index` values. `assert_all_used()` raises a safe `fixture_unused`
assertion if definitions remain unused. Identical requests may reuse the same
static response; their counts increase. Construct a fresh transport for each
evaluation case. Unused definitions are not automatically asserted when a client
closes, since PTV functions open/close clients between calls.

For calls through the agent tool adapters, freeze the scenario retrieval time:

```python
from agent_tools import ToolContext, execute_tool

fixture = FixtureTransport.from_file("evals/fixtures/richmond_30_seconds_late.json")
context = ToolContext(config=fixture.config, transport=fixture)
with fixture.reference_time():
    stations = execute_tool("search_stations", '{"query":"Richmond"}', context)
    result = execute_tool("get_departures_with_disruptions", '{"stop_id":1101,"limit":5}', context)
fixture.assert_all_used()
assert result["retrieved_at_utc"] == fixture.as_of_utc.isoformat()
```

The time scope affects `agent_tools.datetime.now()` only in the current execution
context. Unrelated threads/contexts use the original clock. Nested or overlapping
scopes keep their own reference times and restore the original module attribute
after the final exit, including exceptions. The global Python clock and network
timeout clocks remain untouched. Direct PTV/service calls do not produce
`retrieved_at_utc` and do not require this time scope.

Run the focused offline tests:

```powershell
uv run pytest -q tests/test_fixture_transport.py
```

These tests block real network transports and environment credential loading.
They cover exact response bodies, query distinctions, authentication handling,
safe failures, unused definitions, clock isolation and all nine golden scenarios
through the real PTV/client/service adapters. The transport can be used
independently of the single-case runner below.

## Agent evaluation runner

`evals/run.py` executes the existing agent using a real OpenAI client and fixed
PTV responses. Select one dataset case, or explicitly request all cases in dataset
order. No accuracy/grounding scoring or manual review features are included.

Set `OPENAI_API_KEY` and `OPENAI_MODEL` in the root `.env`, then explicitly run:

```powershell
uv run python -m evals.run --case richmond_30_seconds_late
```

To explicitly run all cases in the current dataset sequentially (now nine):

```powershell
uv run python -m evals.run --all
```

`--all` and `--case` are mutually exclusive. `--case` accepts any existing dataset
case ID. Omitting both retains the original `richmond_30_seconds_late` default.

You can override the configured model with `--model`. Only this explicit CLI
entry point constructs a live OpenAI client. Importing the module or running
pytest does not start a real LLM evaluation. The CLI uses the same 30-second
timeout and disabled SDK retries as the terminal agent.

The runner loads the selected dataset case, verifies its fixture ID/reference
time, and creates `FixtureTransport`. It passes that transport and its dummy
PTV configuration to the existing `answer_question()`. All PTV HTTP requests
remain in memory; real PTV credentials are not required. The reference-time scope
freezes tool `retrieved_at_utc`, while execution duration and run-start time use
the real clock.

`RecordingClient` delegates Responses requests to the actual SDK and returns
each SDK response unchanged. It observes requested tool calls separately from
`AgentTrace`, which records validated arguments and execution outcomes. Golden
facts and prohibited claims are not sent to the LLM. Production agent behaviour,
tool definitions, matching and calculations are unchanged.

For `--all`, the OpenAI client is shared, but each case gets a new recording
wrapper, agent invocation, trace and `FixtureTransport`. Tool history and known
station IDs start empty for every question. Each fixture's reference-time scope
is restored before the next case. An individual failure does not stop later cases.

The initial baseline's observed "live data" wording is preserved. This runner
does not rewrite final answers or add instructions to correct that grounding issue.

Single-case invocations retain their existing output layout:

```text
evals/results/<UTC timestamp>_<unique suffix>/result.json
```

All-case runs use a common run ID and save each case separately:

```text
evals/results/<run_id>/
    summary.json
    cases/
        richmond_departures/result.json
        unknown_station_xyzabc/result.json
        richmond_30_seconds_late/result.json
        glen_waverley_no_estimate/result.json
        flinders_street_morning_late_night_notice/result.json
```

Every case file contains the common `run_id` and its own `case_id`. The CLI prints
the summary path; `summary.cases[].result_file` gives each case's relative path
from the run directory. Previous single-case results are not rewritten. All
generated results remain Git-ignored. Each case result contains:

- Case ID, question, run ID, start time and agent execution duration.
- Dataset version, fixture reference and fixed scenario time.
- Requested model, instruction/tool-schema hashes and the tool-round limit.
- `requested_tool_calls`: model-requested tool name, call ID and selected argument
  fields, before production validation. Malformed JSON is flagged without copying
  raw text; unexpected argument fields and unsupported values are omitted.
- `execution_trace`: the existing sanitized `AgentTrace`, including validated
  arguments, returned records, delay interpretation and relevance annotations.
- `final_answer`: the sanitized final text, or `null` if execution failed.
- `model_responses`: response IDs, returned model identifiers and per-response
  input/output/total token counts where available. Missing usage remains `null`.
- `fixture_coverage`: response match counts, unused request definitions and misses.
- A controlled error code/message when execution cannot complete.

The trace includes validation rejections; not every trace entry represents a PTV
request. A call rejected before dispatch can appear only in requested calls.
A fixture miss can interrupt a tool before its result reaches the trace, so the
saved requested calls, partial trace and fixture coverage should be read together.

`status: completed` means the agent returned an answer. It does not certify
correctness, grounding, relevance or complete fixture usage. Controlled execution
failures still save partial results and return CLI exit status 1; completed runs
return 0. An all-case run returns 1 after attempting all cases if any case has an
error, otherwise 0. Case setup failures are saved with `case_setup_error`, unknown
fixture coverage and no model metadata; other cases continue. Result-save errors
retain captured data and token usage where possible, and an unwriteable case
result is identified by a null `result_file` in the summary. Global configuration,
invalid dataset identifiers and an unavailable output directory produce a safe
CLI error before cases can run.
The runner never saves raw SDK responses, reasoning items, authenticated request
URLs or exception diagnostics. Selected model text uses the existing trace
redaction policy, exposed through `agent_trace.redact_text()`.

The all-case `summary.json` records:

- `cases_attempted`, `cases_completed` and `cases_with_errors`: execution counts.
- `fixture_misses`: the sum of recorded fixture request misses.
- `duration_seconds`: elapsed time across the sequential case loop, including
  case loading and saving. Per-case duration continues to measure agent execution.
- `total_token_usage`: sums of reported input/output/total token counts, including
  available usage from failed cases. A total is null if no values were reported.
- `token_usage_reporting`: retained model request count, responses with complete
  usage, responses missing usage and cases without retained metadata. Totals are
  partial when metadata is missing; absent usage is never treated as zero.
- `cases`: case IDs, execution statuses, result paths, durations and error codes.
- Run ID, start time, dataset version, model and prompt/tool configuration hashes.

These are execution summaries, not accuracy or grounding scores. Inspect each
case's `requested_tool_calls`, `execution_trace`, `final_answer` and
`fixture_coverage` together to review what the model requested, what executed and
what information supported its answer.

An abbreviated synthetic result looks like this; actual files contain the full
sanitized tool records and one metadata entry per model request:

```json
{
  "case_id": "richmond_30_seconds_late",
  "status": "completed",
  "duration_seconds": 1.25,
  "configuration": {"model_requested": "test-model"},
  "requested_tool_calls": [
    {"tool_name": "search_stations", "call_id": "call-1", "arguments": {"query": "Richmond"}},
    {"tool_name": "get_departures_with_disruptions", "call_id": "call-2", "arguments": {"stop_id": 1101, "limit": 5}}
  ],
  "execution_trace": {
    "status": "completed",
    "error_code": null,
    "tool_calls": [
      {"tool_name": "search_stations", "call_id": "call-1", "arguments": {"query": "Richmond"}, "ok": true},
      {"tool_name": "get_departures_with_disruptions", "call_id": "call-2", "arguments": {"stop_id": 1101, "limit": 5}, "ok": true}
    ]
  },
  "final_answer": "Estimated 30 seconds late relative to the schedule.",
  "model_responses": [
    {"model": "test-model-snapshot", "token_usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}}
  ],
  "fixture_coverage": {"match_counts": [1, 1], "unused_requests": [], "miss_count": 0},
  "error": null
}
```

Each actual trace entry also includes its sequence and sanitized structured
result, including the departure's `delay_seconds: 30.0` and `delay_status: late`.

Run the offline runner tests:

```powershell
uv run pytest -q tests/test_evaluation_run.py
uv run pytest -q
```

Tests use scripted responses or the installed SDK with a mock HTTP transport.
Live client creation, network transports and PTV environment credential loading
are blocked in the runner tests, except for the explicitly mocked CLI factory.
They verify successful persistence, requested/validated argument differences,
clock restoration, partial failures, coverage, metadata and redaction. All-case
tests additionally cover case isolation, common run IDs, continuing after failures,
usage aggregation and preserving baseline answers/instructions. No real LLM or
PTV calls are made by pytest.

## Offline baseline scoring

`evals/checks.py` reads existing batch results and the independent golden dataset.
It imports no agent, SDK, model or service calculation and makes no API calls.
It leaves original evaluation JSON, fixtures, golden expectations and production
code unchanged. To score the first five-case baseline:

```powershell
uv run python -m evals.checks --run-dir evals/results/20261118T021405Z_a694484b68e5
```

Reports are saved under the selected run's Git-ignored `scores/` directory:

- `cases.csv`: one row per case listed in the run, including its question,
  source-result path, final answer, mechanical metrics and manual review fields.
- `checks.json`: detailed checks with expected/actual values, call IDs where
  applicable, source trace paths, fixture references and review guidance.
- `aggregate.json`: per-metric case denominators, pass/failure counts, check-level
  totals, failed case IDs, manual review coverage and dataset fingerprint.

The metrics have deliberately limited scopes:

| Metric | Check denominator | What a pass establishes |
| --- | --- | --- |
| `tool_selection` | One check per case | The requested tool names/order exactly match an allowed sequence |
| `arguments` | One check per required call in the selected sequence | Exact argument keys, integer IDs/limits and accepted query spelling |
| `execution` | Case-level prerequisites plus one check per required tool call | Completed execution, consistent case identity, successful correlated tool results, no extra/forbidden calls, no fixture misses and all required fixtures used |
| `source_facts` | Independently specified golden fields | Saved structured source records match the specified golden facts; this does not grade the final prose |
| `timing_wording` | Departures with a golden `delay_description` | The complete supplied sentence occurs after Markdown/whitespace normalization; this is a wording contract, not a semantic truth check |

Each metric also reports its case denominator: cases where at least one check
applies. A case passes that metric only when all its applicable checks pass.
Missing result files remain in these denominators and fail required checks.
`not_applicable` is separate from pass. No combined answer-correctness score is
calculated from these mechanical checks.

By default, `expected_calls` is the only allowed sequence. A golden case may
explicitly add `allowed_call_sequences`, a list of alternative full call sequences
using the same `{name, arguments}` records. Each expected search call can define
`query_variants`, for example:

```json
{"name": "search_stations", "arguments": {"query": "Richmond"}, "query_variants": ["Richmond Station", "richmond"]}
```

Query leading/trailing whitespace is ignored, matching the production validator.
Other aliases or casing changes require explicit variants. IDs and departure
limits accept no numeric-string, float or boolean coercion. Limits are compared
with the golden call, including explicitly requested counts. Declaring a scoring
variant does not change fixture transport matching or production behaviour.

All fixture definitions are required by default. A case can explicitly declare
one-based `required_fixture_indices` when alternatives make some definitions
optional. Fixture coverage is checked against those required indices. Extra
tool names/counts and forbidden calls are checked independently; call order is
assessed by `tool_selection`.

Source comparisons use successful tool outputs and correlate departures by
`run_ref`, notices by `disruption_id`, and associations by linked run references.
They compare UTC/local timestamps, numerical delay fields, missing values,
notice IDs/statuses and structured relevance values with the literal golden
expectations. They never regenerate expected labels using production functions.
Natural-language `outcome`, notice `described_scope` and
`immediate_passenger_impact` expectations remain manual review criteria.

The first saved five-case baseline yields:

| Metric | Passing cases | Passing checks |
| --- | --- | --- |
| Tool selection | 5/5 | 5/5 |
| Arguments | 5/5 | 9/9 |
| Execution completeness | 5/5 | 34/34 |
| Structured source facts | 5/5 | 123/123 |
| Timing wording contract | 2/2 applicable; 3 N/A | 2/2 |

These results do not certify that all five answers are correct or grounded. In
particular, the generic no-estimate sentence satisfies the current literal
contract, but its precision still needs human assessment. The late-night notice's
morning relevance also requires review of the complete answer and source data.
A correct sentence can coexist with a contradictory or unsupported statement;
sentence presence alone is never reported as full factual accuracy or grounding.

The manual CSV columns are `manual_factual_accuracy`, `manual_grounding`,
`manual_response_relevance` and `reviewer_notes`. Scores start blank in CSV and
`null` in JSON, with `review_status: manual_review_pending`. Assign 0 for major
problems, 1 for partially satisfactory quality, or 2 for satisfactory quality,
using the question, source data, `required_response_notes` and `prohibited_claims`
as a review rubric. Notes can explain ambiguity, unsupported impact claims,
mispaired departure facts or fixture-backed information described as live.

Edit these columns in `cases.csv`, then rerun the same scoring command to update
aggregates. Valid manual scores (including zero) and notes are preserved;
invalid scores or mismatched run/case IDs stop scoring without overwriting them.
Manual coverage is reviewed cases divided by all cases, separately for each
dimension, plus the count with all three labels filled. Unreviewed scores are
not counted as zero. No automatic check fills a manual quality label.

To investigate a failure, follow `failed_check_ids` from the CSV into
`checks.json`. Each check identifies the expected/actual values and a path into
the original `execution_trace`, with `call_id` for tool correlation. Open the
linked result and fixture, then compare the raw source record with the golden
fact and the final answer. This keeps execution success separate from whether
the answer uses that evidence accurately.

Run the offline scorer tests:

```powershell
uv run pytest -q tests/test_evaluation_checks.py
```

Tests cover declared alternatives, query variants, exact IDs/limits, missing or
extra calls, coverage failures, missing results, source-data mismatches, limited
wording contracts, manual-label preservation and report denominators. Network
transports and socket creation are blocked in these tests.

## Optional agent execution tracing

Tracing is disabled by default. The terminal command still prints only the final
answer unless you explicitly add `--trace`:

```powershell
uv run python ask_hopwise.py --trace "What are the next trains from Richmond?"
```

With the flag, the answer goes to stdout and a formatted JSON trace goes to
stderr. The trace is printed after the answer; a controlled execution failure
still prints the available partial trace. This command uses your existing `.env`
configuration and makes live API requests. No trace files are created automatically.

Both `answer_question()` and `answer_question_from_env()` still return a string.
Python callers can opt in by supplying a collector:

```python
from agent import answer_question_from_env
from agent_trace import AgentTrace

trace = AgentTrace()
answer = answer_question_from_env("What are the next trains from Richmond?", trace=trace)
records = trace.as_dict()
```

Each invocation resets its supplied collector. `as_dict()` returns an independent
snapshot with this structure:

| Field | Meaning |
| --- | --- |
| `status` | `pending` before execution, then `running`, `completed` or `failed` |
| `error_code` | A fixed code for a fatal configuration, LLM or protocol failure; otherwise `null` |
| `tool_calls` | Ordered tool attempts dispatched through the existing validation/execution function |

Each `tool_calls` entry contains:

- `sequence`: one-based execution/attempt order.
- `tool_name` and `call_id`: the declared tool and the ID used to correlate its output.
- `arguments`: values from the actual Pydantic validation step. Invalid arguments
  and unknown tools use `null`; raw rejected input is never copied. Unknown tool
  names are represented as `[unknown tool]`.
- `ok`: whether that tool attempt succeeded.
- `result`: a sanitized projection of the structured tool result. Failures retain
  their error code, without raw exception messages.

A run can be `completed` even if a tool failed: the model may finish by explaining
that information is unavailable. A rejected attempt does not mean PTV was called.
Protocol failures rejected before dispatch, such as exceeding the tool-round
limit, appear in the top-level `error_code` without an extra tool entry.

Departure records retain stop/route/run identifiers, full scheduled/estimated UTC
timestamps, Melbourne display timestamps, `delay_seconds`, `delay_status`,
`delay_description`, original disruption
IDs, resolved notices and unresolved IDs. Associated, contextual and route-level
notice collections remain separate. Missing estimates and delays remain `null`.
For example, a synthetic 09:58:00 schedule and 09:58:30 estimate appear with
`delay_seconds: 30.0` in the trace; tracing does not calculate or interpret new
departure or disruption information.

Tracing uses an allowlist of result fields. Configuration/client objects, SDK
diagnostics, prompts, final-answer text and raw invalid arguments are excluded.
URL fields are omitted, URLs embedded in text are removed, and known credential
values and credential assignments are redacted. Redaction values are used only
while creating snapshots and are not retained in the collector. The original
tool result sent to the LLM is unchanged, and trace data is never sent to the LLM.
Streamlit continues to use the default untraced entry point in this increment.

Run the focused offline tracing tests:

```powershell
uv run pytest -q tests/test_agent_trace.py tests/test_ask_hopwise.py
```

To inspect a complete synthetic search -> departures -> final-answer execution,
including the 30-second timing example, run:

```powershell
uv run pytest -v -s tests/test_agent_trace.py::test_trace_records_search_and_departures
```

The printed JSON comes from the real trace collector with mocked LLM and PTV
services. The tests cover default-off behaviour, validated arguments, record
contents, rejected calls, partial failures, credential redaction and unchanged
model requests and service execution.

## Streamlit AI Assistant

The existing station search, departure table and disruption expanders remain
available. Below them, **Hopwise AI Assistant** accepts a natural-language question
and displays the agent's final answer. Configure `OPENAI_API_KEY` and `OPENAI_MODEL`
in the root `.env`, alongside the existing PTV credentials, then run:

```powershell
uv run streamlit run streamlit_app.py
```

Try: "What are the next trains from Richmond, and are there any disruptions?"
Submitting a chat question makes live OpenAI/PTV requests. Opening the app alone
does not create an OpenAI client or require OpenAI configuration, so the manual
station/departure interface can still be used independently.

The chat follows this flow:

```text
Chat question -> agent.answer_question_from_env() -> answer_question()
              -> existing tool adapters -> existing PTV services/models
              -> final answer -> chat display and session state
```

`answer_question_from_env()` loads local configuration, opens the OpenAI client
and calls the existing orchestration loop. The terminal script uses the same
entry point. Streamlit manages rendering and widget/session state; it does not
define tools, execute a tool-calling loop or duplicate API logic.

`st.session_state.chat_messages` stores user and assistant messages for the
current browser session. Streamlit redraws those messages on each rerun and
calls the agent only for a newly submitted question. Station searches and
departure refreshes preserve the chat without repeating the agent request.
The history is for display: each question is an independent agent run, so ask
a complete question rather than relying on an earlier chat message or the
manually selected station. Messages are not saved to disk or shared between
browser sessions.

A spinner remains visible while the agent and its tools execute. Configuration,
LLM and propagated PTV failures show a fixed friendly message instead of raw
exception diagnostics. PTV failures already converted to controlled tool results
are described by the agent in its answer. Credentials stay server-side and are
not placed in chat state or tool results. No developer/debug view is added in
this increment.

Run the focused offline UI tests:

```powershell
uv run pytest -q tests/test_streamlit_assistant.py
```

These `AppTest` tests mock the agent entry point and PTV services. They cover
message persistence, independent questions and sessions, progress indication,
safe error recovery, and coexistence with station selection and departure
refreshes, without making real OpenAI or PTV requests.

## Inspecting the agent orchestration tests

Run the focused orchestration tests with scripted LLM responses and mocked PTV
service functions:

```powershell
uv run pytest -v tests/test_agent.py
```

To inspect the successful search -> departures -> final-answer sequence, disable
pytest's output capture for that test:

```powershell
uv run pytest -v -s tests/test_agent.py::test_complete_loop_preserves_output_items_and_call_ids
```

The printed trace shows the two requested tool names, their JSON arguments and
call IDs, followed by the correlated result statuses. The test uses synthetic
station IDs: it verifies that the returned Richmond ID `4321` is passed to the
departure service, and that search executes before departure retrieval.

`ScriptedClient.tool_calls` records model-requested calls. `client.requests`
captures each complete Responses API request, including the accumulated input
history. `tool_outputs(client.requests[-1])` exposes the results returned to the
model; apply `json.loads(item["output"])` to inspect the full payload. PTV mocks'
`mock_calls` record actual Python service executions, which remain empty for
rejected calls. Requested calls and executed calls differ in rejection tests.

The cases cover unknown tools, malformed and invalid arguments, correcting an
invalid limit before executing a departure query, controlled errors at both PTV
stages and the model boundary, the five-round limit, and final answers that
require no tool execution. A final clarification can complete after one model
response without contacting PTV. All model outputs in these tests are scripted;
no OpenAI/PTV credentials or live calls are needed. The existing SDK compatibility
test in the same file also uses a fake HTTP transport.

References: [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling)
and [OpenAI Python SDK](https://github.com/openai/openai-python).
