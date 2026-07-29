# Mock-web booking — `http_request` against a first-party service

A single-task native pack that drives the first-party **mock-web** service over
the `http_request` builtin. The agent books a hotel room through an HTML form
and reports the confirmation number mock-web issues; grading is deterministic
and keyless-gradable in shape — it asserts the booking round-trip happened and
that the mock-web-issued confirmation number appears in the transcript.

```
agent → http_request → mock-web:8080  (/booking form → POST /booking/confirm)
```

## What this example demonstrates

- **`http_request` against a composed peer, no `environment_manifest`.** The
  task enables only `http_request` (allow-listed to `mock-web:8080`) and
  declares no environment manifest. mock-web is reached by Docker DNS on the
  standalone stack (`deploy/standalone/docker-compose.yaml`), exactly the way
  the bundled `tool_use` pack reaches db-service — the runner's own network
  wires the peer, not a per-task manifest.
- **A form round-trip, not a JSON API.** `/booking/confirm` reads an HTML form
  submission (`request.form()`), so the agent must GET `/booking` to learn the
  fields (`name`, `hotel`, `checkin`, `checkout`) and POST them as **form
  data** — a JSON body would not populate the form and the booking would fail.
- **Deterministic grading on the site-issued booking outcome.** mock-web issues
  the confirmation number `BKSEA12345` from the submitted hotel
  (`BK<hotel[:3].upper()>12345`). The round-trip is enforced by the
  `required_actions` POST gate together with reporting that site-issued token —
  not by the token being unguessable (the template is public). Grading is
  transcript-only:

  | Check | What it asserts |
  |---|---|
  | `required_actions` (POST) | the agent POSTed over `http_request` — the booking round-trip actually happened |
  | `must_contain: BKSEA12345` | the mock-web-issued confirmation number is reported |

  Both are product-scored under `transcript_rules` with `pass_threshold: 1.0`,
  so dropping either — a GET-only trajectory, or a missing confirmation number —
  fails the task.

## Validate

```bash
uv run tolokaforge validate --tasks "examples/native/mock_web_booking/dataset/**/task.yaml"
```

## Run

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/native/mock_web_booking/run_configs/dev.yaml
```

Needs a running Docker daemon with the standalone stack up (so `mock-web:8080`
is reachable on the runner network) and `OPENROUTER_API_KEY` in `.env`.

## Layout

```
examples/native/mock_web_booking/
├── run_configs/dev.yaml          # haiku agent + user, no judge
├── project.yaml                  # discovery glob + native defaults
├── README.md                     # this file
└── dataset/tasks/booking_01/
    ├── task.yaml                 # http_request-only, allow-listed to mock-web:8080
    └── grading.yaml              # transcript-only: POST gate + confirmation-number token
```

## Related

- [`docs/GRADING.md`](../../../docs/GRADING.md) — grading families, including
  `transcript_rules.required_actions` and `must_contain`
- [`../tool_use/README.md`](../tool_use/README.md) — structured tool-call
  grading without a Docker substrate
- [`docs/STANDALONE_RUNNER.md`](../../../docs/STANDALONE_RUNNER.md) — the
  composed standalone stack this pack runs against
