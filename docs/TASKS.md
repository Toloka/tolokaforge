# Task Authoring Guide

This guide explains how to create and organize tasks in Tolokaforge.

## Task Organization

Tasks can be organized in any directory. Example tasks are included in `examples/`:

```bash
# Validate example tasks
uv run tolokaforge validate --tasks "examples/**/task.yaml"
```

Use the `task_packs` configuration to point at any directory containing tasks:
```yaml
evaluation:
  task_packs:
    - "/path/to/your/tasks"
  tasks_glob: "**/task.yaml"
```

## Task Layout

```
<tasks_root>/<category>/<task_id>/
├── task.yaml
├── grading.yaml
├── initial_state.json          # optional
├── www/                        # optional, for full-site browser tasks
├── mock_web/                   # optional, for single-page browser tasks
├── rag/corpus/                 # optional
└── README.md                   # optional
```

Categories: `terminal`, `browser`, `mobile`. Use `mobile` for app-style tasks that simulate phone interactions (restricted browser actions, no URL navigation). Use `browser` for full web browsing tasks. The mock-web service discovers static files from all categories automatically.

## task.yaml Essentials

```yaml
task_id: "shopping_review"
name: "Submit Product Review"
category: "browser"
description: "Submit a 4-star review on a mock website"

initial_state:
  json_db: "initial_state.json"
  mock_web:
    base_url: "http://mock-web:8080"
  filesystem:
    copy: []
  rag:
    corpus_dir: "rag/corpus"

tools:
  agent:
    enabled: ["browser", "db_query", "db_update"]
  user:
    enabled: []

user_simulator:
  mode: "llm"
  persona: "online shopper"
  backstory: |
    You bought a coffee maker and want to leave a 4-star review.
    When the agent confirms the review is submitted, say ###STOP###.

grading: "grading.yaml"
```

## Initial State

- `json_db`: JSON file loaded into the JSON DB service. Use this for any task state that needs to be verified by grading.
- `filesystem.copy`: files copied into `/env/fs/agent-visible`.
- `mock_web.base_url`: base URL for mock web service (`http://mock-web:8080`).
- `rag.corpus_dir`: directory of `.txt` files for RAG indexing.

## User Simulator

Prefer LLM mode (`mode: "llm"`) for realistic conversations. Use `backstory` to define the user's goal and information they reveal over the conversation:

```yaml
user_simulator:
  mode: "llm"
  persona: "impatient customer"
  backstory: |
    You need to reschedule your delivery to next Tuesday.
    Do not reveal all details at once — answer the agent's questions naturally.
    When the agent confirms the reschedule, say ###STOP###.
```

Scripted mode (`mode: "scripted"`) is available for simple deterministic flows but produces less realistic conversations.

## Browser vs Mobile Tool

Use `browser` for full web browsing tasks (URL navigation, search). Use `mobile` for phone app tasks (no URL bar, mobile viewport).

```yaml
# Browser task - full web browsing
tools:
  agent:
    enabled: ["browser"]
    browser:
      initial_url: "http://mock-web:8080"   # Optional

# Mobile task - phone app interaction
tools:
  agent:
    enabled: ["mobile"]
    mobile:
      apps:
        DoorDash: "http://mock-web:8080"
      initial_app: "DoorDash"
```

The `mobile` tool uses a phone-sized viewport (412x915) and only exposes tap, type, scroll, and gesture actions — no URL navigation, search, or browser-specific actions. See [BROWSER_TOOLS.md](BROWSER_TOOLS.md) for full details.

## Mobile App Fixtures

Mobile app tasks in `tasks/mobile/` share a common mock data layer and theming approach:

- **Apps live in** `tasks/mobile/app_*` with static assets under `www/<domain>/`.
- **Brand variants** live in `brand/real.json` and `brand/fictional.json`. Set with `?brand=real` or `?brand=fictional`.
- **Shared dataset** lives in `tasks/mobile/_data/v1/` and is served by the mock-web API:
  - `GET /api/app-data?app=<app>&dataset=v1`
  - Files: `places.json`, `menus.json`, `hours.json`, `reviews.json`, `reservations.json`, `grocery_items.json`, `coffee_menu.json`, `events.json`, `notes.json`
  - Prefer deterministic values (e.g., `open_now`) instead of real-time clocks.
- **JSON DB conventions** for grading: `orders`, `grocery_orders`, `coffee_orders`, `reservations`, `searches`, `shortlists`, `calendar_events`, `notes`.

When authoring multi-app tasks, reuse the same `place_id` or item IDs across apps so the agent must reconcile information rather than guess.

## Mobile Benchmark Suite

The repository includes a 50-task mobile benchmark pack under `tasks/mobile/` (exclude `_templates` and the `app_*` fixtures). To run the full suite, point `tasks_glob` at the task folders:

```yaml
models:
  agent:
    provider: openrouter
    name: anthropic/claude-3.5-sonnet
    temperature: 0.0
  user:
    provider: openrouter
    name: anthropic/claude-3.5-sonnet
    temperature: 0.7

evaluation:
  tasks_glob: "tasks/mobile/*/task.yaml"
  output_dir: "results/mobile_benchmark"

orchestrator:
  repeats: 1
  max_turns: 25
```

To run a single task, change `tasks_glob` to its folder (e.g., `tasks/mobile/maps_opentable_calendar_sakura_dinner/task.yaml`).

## Browser Tasks

- Place HTML/JS/CSS in a `www/<sitename>/` subdirectory for full-site tasks, or `mock_web/` for single-page tasks.
- The mock web service serves files from `www/` subdirectories at `http://mock-web:8080/`.
- Use `initial_state.json` and JSON DB for any state that needs to be graded. HTML/JS should write to JSON DB, not to local files.
- Avoid external URLs — the environment network is sandboxed.

## Grading Tips

- Prefer `state_checks.jsonpaths` for deterministic, objective checks.
- Use `transcript_rules` to enforce tool usage patterns.
- Use `llm_judge` only for genuinely subjective evaluation (not as a softener for weak state checks).
- For RL training value, use strict grading: `state_checks` weight 1.0, no LLM judge padding.

See `docs/REFERENCE.md` for full schemas.

---

## Designing Challenging Tasks

Tasks that always pass (100% success rate) provide zero RL training signal. Tasks that never pass (0%) are broken. Target **30-70% pass rate** for maximum training value.

### Anti-Patterns (make tasks trivially easy)

- **Step-by-step instructions in user messages.** "1. Navigate to website 2. Click button 3. Fill form" turns the agent into a script executor. Use natural language: "I need to update my shipping address."
- **UI defaults that satisfy grading.** If grading checks for "Apple Pay" and the checkout page defaults to Apple Pay, the agent doesn't need to do anything. Defaults should be DIFFERENT from the graded values.
- **System prompt escape hatches.** "If you can't do X, do Y instead" gives the agent permission to skip the hard part. The system prompt should describe capabilities, not workarounds.
- **Overly broad scripted_flow triggers.** Generic words like "done", "confirmed", "success" end the conversation before the agent finishes. Use specific triggers (order IDs, exact phrases) or use LLM user simulator.
- **LLM judge with high weight as a softener.** An LLM judge giving 0.7 for "attempted the task" masks state_checks failures. Reserve LLM judge for genuinely subjective evaluation.
- **JavaScript safety nets.** Code like `value || 'correct_answer'` means the graded value is always correct regardless of agent action. Record what actually happened.

### Patterns for Effective Difficulty

- **Require active, non-default choices.** If a form has a default value, grade for a different value that requires explicit selection. Size "Large" when default is "Small". Payment "Apple Pay" when default is "Credit Card".
- **Natural language user messages.** Use LLM user simulator with a backstory that reveals information gradually, like a real person would.
- **Minimal system prompts.** Don't teach the agent the solution. Describe what tools are available, not how to use them for this specific task.
- **Multi-step reasoning.** Require the agent to gather information from one place and apply it in another (e.g., look up an order number in a PDF, then use it in a portal).
- **Strict state_checks grading.** Weight 1.0 on state_checks with exact-match assertions. No partial credit for vague attempts.
- **App-style browser tasks.** Use `initial_url` and `allowed_actions` to create phone-app-like experiences. Remove `navigate` and `open_web_browser` so the agent must interact with the UI, not bypass it with URLs.

### Calibration

After creating a task:

1. Run it 5+ times with the target model.
2. If pass rate is 100%: the task is too easy — add requirements, remove defaults, tighten grading.
3. If pass rate is 0%: the task is broken or impossible — verify the HTML flow works manually, check grading assertions match actual data format.
4. Target 30-70% for RL training value.
