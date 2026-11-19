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
| [agent_tools.py](../agent_tools.py) | Validate tool arguments and serialize service results for the LLM |
| [agent.py](../agent.py) | Execute the bounded Responses API tool-calling loop |
| [agent_trace.py](../agent_trace.py) | Capture optional sanitized execution snapshots |
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
supplies its wording without changing that calculation:

| Difference | Meaning |
| --- | --- |
| Positive | Estimated late relative to the schedule |
| Negative | Estimated early relative to the schedule |
| Zero | Estimate matches schedule; no guarantee of on-time operation |
| Missing estimate or schedule | Unknown comparison, represented as `None`/`null` |

The tool adapter also supplies `departure_time_description`: an available estimate
comes first, the schedule remains labelled reference information, and a missing
estimate is explicitly unavailable. It never fills an estimate from the schedule.
The agent is instructed to use the supplied descriptions verbatim; final LLM
compliance remains an evaluation concern.

Relevance annotations compare complete date bounds with the departure timestamp
and identify explicitly affected boarding/other stop IDs. Missing bounds or
location data remain unknown. These annotations preserve associations and PTV
statuses. They do not infer recurring overnight windows, delays, cancellations
or causes from notice prose.

## UI state, tracing and credentials

Streamlit session state preserves the selected stop, fetched board, successful
refresh time and displayed chat history across reruns. Departures load on station
selection or explicit refresh. A failed refresh retains the earlier board with a
warning. Chat history is for display: each question starts a new agent context
and does not inherit the selected station or previous messages.

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
