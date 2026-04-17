"""Mobile tool - phone app interaction via restricted browser"""

import json
import re
from typing import Any

from tolokaforge.tools.builtin.browser import BrowserTool
from tolokaforge.tools.registry import ToolResult


class MobileTool(BrowserTool):
    """Phone app interaction tool with mobile viewport and app-based navigation.

    Wraps BrowserTool with phone-appropriate constraints:
    - Mobile viewport (412x915, typical modern smartphone)
    - App-based navigation (open_app) instead of URLs
    - No URL bar, no browser navigation, no search
    - Tasks configure available apps as a name-to-URL mapping
    """

    # Actions available on a phone (no URL bar, no hover)
    MOBILE_ACTIONS = [
        "open_app",
        "click_at",
        "type_text_at",
        "scroll_document",
        "scroll_at",
        "key_combination",
        "wait_5_seconds",
        "go_back",
        "drag_and_drop",
        "select",
        "press_enter",
    ]

    # Phone viewport (CSS pixels, typical modern smartphone)
    PHONE_WIDTH = 412
    PHONE_HEIGHT = 915

    def __init__(
        self,
        apps: dict[str, str],
        initial_app: str | None = None,
        screenshots_dir: str = "/tmp/screenshots",
        db_base_url: str | None = None,
        visual_mode: bool = True,
    ):
        # Resolve initial_url from initial_app
        initial_url = apps.get(initial_app, "") if initial_app else None

        super().__init__(
            screenshots_dir=screenshots_dir,
            viewport_width=self.PHONE_WIDTH,
            viewport_height=self.PHONE_HEIGHT,
            initial_url=initial_url,
            allowed_actions=self.MOBILE_ACTIONS,
            visual_mode=visual_mode,
            db_base_url=db_base_url,
        )
        self.name = "mobile"
        self.description = (
            "Interact with apps on a phone using tap, type, scroll, and app switching"
        )
        self.apps = apps  # app_name -> URL mapping

    def get_schema(self) -> dict[str, Any]:
        """Schema with app-based navigation instead of URLs."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "actions": {
                            "description": "Sequence of actions to perform on the phone",
                            "anyOf": [
                                {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "type": {
                                                "type": "string",
                                                "enum": self.allowed_actions,
                                            },
                                            "app_name": {
                                                "type": "string",
                                                "enum": list(self.apps.keys()),
                                                "description": "App to open (for open_app action)",
                                            },
                                            "x": {
                                                "type": "integer",
                                                "minimum": 0,
                                                "maximum": 1000,
                                                "description": "X coordinate in 1000x1000 grid",
                                            },
                                            "y": {
                                                "type": "integer",
                                                "minimum": 0,
                                                "maximum": 1000,
                                                "description": "Y coordinate in 1000x1000 grid",
                                            },
                                            "destination_x": {
                                                "type": "integer",
                                                "minimum": 0,
                                                "maximum": 1000,
                                                "description": "Destination X for drag_and_drop",
                                            },
                                            "destination_y": {
                                                "type": "integer",
                                                "minimum": 0,
                                                "maximum": 1000,
                                                "description": "Destination Y for drag_and_drop",
                                            },
                                            "text": {
                                                "type": "string",
                                                "description": "Text to type for type_text_at or option label for select",
                                            },
                                            "press_enter": {
                                                "type": "boolean",
                                                "description": "Press Enter after typing (default: True)",
                                            },
                                            "clear_before_typing": {
                                                "type": "boolean",
                                                "description": "Clear field before typing (default: True)",
                                            },
                                            "keys": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                                "description": "Key tokens (e.g., ['CTRL', 'C'])",
                                            },
                                            "direction": {
                                                "type": "string",
                                                "enum": ["up", "down", "left", "right"],
                                                "description": "Scroll direction",
                                            },
                                            "magnitude": {
                                                "type": "integer",
                                                "minimum": 0,
                                                "maximum": 1000,
                                                "description": "Scroll magnitude (default: 800)",
                                            },
                                        },
                                        "required": ["type"],
                                    },
                                },
                                {
                                    "type": "string",
                                    "description": "JSON array of action objects (comments are ignored).",
                                },
                            ],
                        }
                    },
                    "required": ["actions"],
                    "additionalProperties": False,
                },
            },
        }

    def _normalize_actions_payload(self, actions: Any) -> list[dict[str, Any]]:
        if isinstance(actions, dict):
            return [actions]
        if isinstance(actions, list):
            return actions
        if isinstance(actions, str):
            cleaned = re.sub(r"//.*?$", "", actions, flags=re.MULTILINE).strip()
            if not cleaned:
                raise ValueError("Actions string is empty after cleaning.")
            try:
                parsed = json.loads(cleaned)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Actions string is not valid JSON: {exc}") from exc
            if isinstance(parsed, dict):
                if "actions" in parsed:
                    parsed = parsed["actions"]
                else:
                    return [parsed]
            if not isinstance(parsed, list):
                raise ValueError("Parsed actions must be a list.")
            return parsed
        raise ValueError("Actions must be a list or JSON string.")

    def execute(self, actions: Any) -> ToolResult:
        try:
            normalized = self._normalize_actions_payload(actions)
        except ValueError as exc:
            return ToolResult(success=False, output="", error=str(exc))
        if not normalized:
            return ToolResult(
                success=False,
                output="",
                error="At least one action is required per mobile tool call.",
            )
        return super().execute(normalized)

    async def _execute_actions(self, actions: list[dict[str, Any]]) -> tuple[bool, str, str]:
        """Execute actions, handling open_app by mapping to URL navigation."""
        resolved_actions = []
        for action in actions:
            for key in ("x", "y", "destination_x", "destination_y"):
                if key in action and isinstance(action[key], (int, float)):
                    action[key] = max(0, min(1000, int(action[key])))
            if action.get("type") in {"scroll_document", "scroll_at"} and "direction" not in action:
                action["direction"] = "down"
            if action.get("type") in {"click_at", "scroll_at"}:
                action.setdefault("x", 500)
                action.setdefault("y", 500)
            if action["type"] == "open_app":
                app_name = action.get("app_name", "")
                url = self.apps.get(app_name)
                if not url:
                    available = ", ".join(self.apps.keys())
                    return False, "", f"Unknown app: {app_name}. Available apps: {available}"
                # Convert to a navigate action for the parent class
                resolved_actions.append({"type": "navigate", "url": url})
            elif action["type"] == "select":
                resolved = {"type": "select"}
                if "x" in action:
                    resolved["x"] = action["x"]
                if "y" in action:
                    resolved["y"] = action["y"]
                if "text" in action:
                    resolved["text"] = action["text"]
                elif "option" in action:
                    resolved["text"] = action["option"]
                elif "value" in action:
                    resolved["text"] = action["value"]
                resolved_actions.append(resolved)
            elif action["type"] == "press_enter":
                resolved_actions.append({"type": "key_combination", "keys": ["ENTER"]})
            else:
                resolved_actions.append(action)
        return await super()._execute_actions(resolved_actions)
