# Browser & Mobile Tool Guide

Tolokaforge ships two Playwright-based tools for UI interaction:

- **`browser`** — Full web browser with URL navigation, search, and all actions. For tasks where the agent browses websites.
- **`mobile`** — Phone app interaction with a mobile viewport (412x915) and no URL navigation. For tasks that simulate phone apps.

Both use the Gemini Computer Use API action format with a 1000x1000 coordinate grid.

---

## Browser Tool

### Enabling

```yaml
tools:
  agent:
    enabled: ["browser"]
```

### Per-Task Configuration

The browser tool supports optional per-task configuration:

```yaml
tools:
  agent:
    enabled: ["browser"]
    browser:
      initial_url: "http://mock-web:8080"
      allowed_actions:
        - click_at
        - type_text_at
        - scroll_document
        # ... subset of actions
```

- **`initial_url`**: Pre-navigates the browser before the agent's first turn.
- **`allowed_actions`**: Restricts available action types. If omitted, all 14 actions are available.

### All Browser Actions

| Action | Parameters | Description |
|--------|-----------|-------------|
| `open_web_browser` | none | Open/initialize the browser |
| `navigate` | `url` | Navigate to a URL |
| `click_at` | `x`, `y` | Click at grid coordinates |
| `hover_at` | `x`, `y` | Hover at grid coordinates |
| `type_text_at` | `x`, `y`, `text`, `clear_before_typing`, `press_enter` | Type text at coordinates |
| `key_combination` | `keys` | Press key combination (e.g., `["CTRL", "C"]`) |
| `scroll_document` | `direction` | Scroll the page (`up`, `down`, `left`, `right`) |
| `scroll_at` | `x`, `y`, `direction`, `magnitude` | Scroll at specific coordinates |
| `go_back` | none | Browser back button |
| `go_forward` | none | Browser forward button |
| `search` | `query` | Browser address bar search |
| `select` | `x`, `y`, `text` | Select option at coordinates by label/value text |
| `wait_5_seconds` | none | Wait 5 seconds |
| `drag_and_drop` | `x`, `y`, `destination_x`, `destination_y` | Drag from one point to another |

---

## Mobile Tool

### Enabling

```yaml
tools:
  agent:
    enabled: ["mobile"]
    mobile:
      apps:
        MyApp: "http://mock-web:8080"
        Settings: "http://mock-web:8080/settings"
      initial_app: "MyApp"
```

- **`apps`**: Maps app names to URLs. The agent sees app names in the tool schema; URLs are never exposed.
- **`initial_app`**: Which app to open before the agent's first turn.

### How It Differs from Browser

| | Browser | Mobile |
|---|---------|--------|
| Viewport | 1440x900 (desktop) | 412x915 (phone) |
| Navigation | URLs (`navigate`, `search`) | Apps (`open_app` with app names) |
| Actions | All 14 | 11 (tap/type/scroll/open_app/back/drag/keys/wait/select/press_enter) |
| Schema | Includes `url`, `query` params | Includes `app_name` param |
| Category | `tasks/browser/` | `tasks/mobile/` |

### Mobile Actions

| Action | Parameters | Description |
|--------|-----------|-------------|
| `open_app` | `app_name` | Switch to an app (names from task config) |
| `click_at` | `x`, `y` | Tap at grid coordinates |
| `type_text_at` | `x`, `y`, `text`, `clear_before_typing`, `press_enter` | Type text at coordinates |
| `scroll_document` | `direction` | Scroll the page |
| `scroll_at` | `x`, `y`, `direction`, `magnitude` | Scroll at specific coordinates |
| `key_combination` | `keys` | Press key combination |
| `select` | `x`, `y`, `text` | Select option at coordinates by label/value text |
| `press_enter` | none | Press Enter key |
| `wait_5_seconds` | none | Wait 5 seconds |
| `go_back` | none | Back button |
| `drag_and_drop` | `x`, `y`, `destination_x`, `destination_y` | Drag gesture |

### System Prompt for Mobile Tasks

Frame the context as a phone app, not a website:

```yaml
policies:
  agent_system_prompt: |
    You are a helpful assistant that can interact with apps on the user's phone.
    The DoorDash app is currently open. Use the mobile tool to tap, type, and scroll within the app.
```

---

## Action Format

Both tools accept an array of actions:

```json
{
  "actions": [
    {"type": "click_at", "x": 512, "y": 420},
    {"type": "type_text_at", "x": 512, "y": 300, "text": "hello"}
  ]
}
```

Coordinates use a 1000x1000 grid mapped to the current viewport.

---

## Mock Web Pages

Both browser and mobile tasks use the mock web service:
- Base URL: `http://mock-web:8080`
- Full-site tasks: place files in `www/<sitename>/` subdirectory
- Single-page tasks: place HTML in `mock_web/` subdirectory
- HTML/JS should write state to JSON DB (not local files) so grading can verify it
- The mock-web service discovers static files from all task categories automatically

Avoid external URLs because the environment network is sandboxed.

---

## Designing Non-Trivial Tasks

- **HTML defaults must NOT satisfy grading criteria.** If you grade for "Apple Pay", the checkout page should default to "Credit Card".
- **Require active UI interactions.** Grade for values that require dropdown changes, checkbox toggles, or modal selections — not whatever loads by default.
- **Use `state_checks` to verify specific values.** Don't just check "something was submitted" — check exact field values.
- **Remove JavaScript safety nets.** Code like `selectedMethod || 'correct_answer'` means the correct value is recorded even without agent action. Record the actual state: `selectedMethod || ''`.
- **Use `mobile` tool for app-style tasks** to prevent the agent from bypassing the UI with direct URL navigation.
