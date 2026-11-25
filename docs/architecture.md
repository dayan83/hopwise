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
