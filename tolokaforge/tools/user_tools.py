"""User-side tools for dual-control scenarios

In dual-control scenarios, both the agent and user can take actions in the environment.
User tools allow the user simulator to perform physical actions (check lights, press buttons, etc.)
that the agent cannot directly perform.
"""

from typing import Any

from tolokaforge.core.env_state import EnvironmentState
from tolokaforge.tools.registry import Tool, ToolResult, sanitize_tool_schema


class CheckDeviceLightsTool(Tool):
    """User tool to check status lights on a device

    Example: In telecom scenarios, user can check if lights are blinking
    indicating network connectivity issues.
    """

    def __init__(self, env_state: EnvironmentState):
        super().__init__(
            name="user_check_device_lights",
            description="Check the status lights on the device to see if any are blinking or off",
        )
        self.env_state = env_state

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "device_id": {
                            "type": "string",
                            "description": "The device to check (e.g., 'modem', 'router', 'set_top_box')",
                        }
                    },
                    "required": ["device_id"],
                },
            },
        }

    def execute(self, device_id: str, **kwargs) -> ToolResult:
        """Check device lights status"""
        try:
            # Get device state from environment
            db = self.env_state.get_db()
            if not db:
                return ToolResult(success=False, output="", error="No database state available")

            devices = db.get("devices", {})
            device = devices.get(device_id)

            if not device:
                return ToolResult(success=False, output="", error=f"Device '{device_id}' not found")

            # Check lights status
            lights = device.get("lights", {})
            status_description = []

            for light_name, light_info in lights.items():
                state = light_info.get("state", "off")
                color = light_info.get("color", "unknown")

                if state == "blinking":
                    status_description.append(f"{light_name} light is blinking {color}")
                elif state == "on":
                    status_description.append(f"{light_name} light is {color}")
                else:
                    status_description.append(f"{light_name} light is off")

            result = f"Device '{device_id}' lights:\n" + "\n".join(status_description)

            # Log user action in environment
            if "user_actions" not in db:
                db["user_actions"] = []
            db["user_actions"].append(
                {"tool": "user_check_device_lights", "device_id": device_id, "result": result}
            )

            return ToolResult(success=True, output=result)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))


class PressButtonTool(Tool):
    """User tool to press a physical button on a device

    Example: Press power button, reset button, WPS button, etc.
    """

    def __init__(self, env_state: EnvironmentState):
        super().__init__(
            name="user_press_button", description="Press a physical button on the device"
        )
        self.env_state = env_state

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "device_id": {
                            "type": "string",
                            "description": "The device with the button (e.g., 'modem', 'router')",
                        },
                        "button_name": {
                            "type": "string",
                            "description": "The button to press (e.g., 'power', 'reset', 'wps')",
                        },
                        "hold_seconds": {
                            "type": "number",
                            "description": "How long to hold the button (default: 0 for quick press)",
                            "default": 0,
                        },
                    },
                    "required": ["device_id", "button_name"],
                },
            },
        }

    def execute(
        self, device_id: str, button_name: str, hold_seconds: float = 0, **kwargs
    ) -> ToolResult:
        """Press a button on a device"""
        try:
            # Get device state from environment
            db = self.env_state.get_db()
            if not db:
                return ToolResult(success=False, output="", error="No database state available")

            devices = db.get("devices", {})
            device = devices.get(device_id)

            if not device:
                return ToolResult(success=False, output="", error=f"Device '{device_id}' not found")

            # Check if button exists
            buttons = device.get("buttons", {})
            if button_name not in buttons:
                available_buttons = ", ".join(buttons.keys())
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Button '{button_name}' not found on device '{device_id}'. Available buttons: {available_buttons}",
                )

            # Simulate button press effect
            button = buttons[button_name]
            effect = button.get("effect", "none")

            result = f"Pressed {button_name} button on {device_id}"
            if hold_seconds > 0:
                result += f" for {hold_seconds} seconds"

            # Apply button effect
            if effect == "power_cycle":
                device["powered_on"] = not device.get("powered_on", True)
                result += f"\nDevice powered {'on' if device['powered_on'] else 'off'}"
            elif effect == "reset":
                device["reset_count"] = device.get("reset_count", 0) + 1
                result += f"\nDevice reset (count: {device['reset_count']})"
            elif effect == "wps":
                device["wps_active"] = True
                result += "\nWPS pairing mode activated"

            # Log user action
            if "user_actions" not in db:
                db["user_actions"] = []
            db["user_actions"].append(
                {
                    "tool": "user_press_button",
                    "device_id": device_id,
                    "button_name": button_name,
                    "hold_seconds": hold_seconds,
                    "effect": effect,
                }
            )

            return ToolResult(success=True, output=result)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))


class CheckDeviceStatusTool(Tool):
    """User tool to check overall device status

    Example: Check if device is on, what display shows, etc.
    """

    def __init__(self, env_state: EnvironmentState):
        super().__init__(
            name="user_check_device_status",
            description="Check the overall status of a device (power, display, connectivity)",
        )
        self.env_state = env_state

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "device_id": {"type": "string", "description": "The device to check"}
                    },
                    "required": ["device_id"],
                },
            },
        }

    def execute(self, device_id: str, **kwargs) -> ToolResult:
        """Check device status"""
        try:
            # Get device state from environment
            db = self.env_state.get_db()
            if not db:
                return ToolResult(success=False, output="", error="No database state available")

            devices = db.get("devices", {})
            device = devices.get(device_id)

            if not device:
                return ToolResult(success=False, output="", error=f"Device '{device_id}' not found")

            # Build status report
            status_lines = [f"Device: {device_id}"]

            # Power status
            powered_on = device.get("powered_on", True)
            status_lines.append(f"Power: {'ON' if powered_on else 'OFF'}")

            # Display
            if "display" in device:
                display_text = device["display"].get("text", "")
                status_lines.append(f"Display: {display_text}")

            # Connectivity
            if "connectivity" in device:
                conn = device["connectivity"]
                for conn_type, conn_status in conn.items():
                    status_lines.append(f"{conn_type.upper()}: {conn_status}")

            # Additional status
            if "status" in device:
                for key, value in device["status"].items():
                    status_lines.append(f"{key}: {value}")

            result = "\n".join(status_lines)

            # Log user action
            if "user_actions" not in db:
                db["user_actions"] = []
            db["user_actions"].append({"tool": "user_check_device_status", "device_id": device_id})

            return ToolResult(success=True, output=result)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))


class UserToolExecutor:
    """Executes user-side tools during dual-control scenarios

    User tools are special tools that the user simulator can invoke
    to perform physical actions that the agent cannot directly perform.

    This executor can work with both hardcoded tools and MCP-based tools.
    """

    def __init__(self, env_state: EnvironmentState = None, use_default_tools: bool = True):
        """Initialize UserToolExecutor

        Args:
            env_state: Optional environment state for default tools
            use_default_tools: If True, register hardcoded example tools
        """
        self.env_state = env_state
        self.tools: dict[str, Tool] = {}
        self.tool_logs: list[dict[str, Any]] = []

        if use_default_tools and env_state:
            self._register_default_tools()

    def _register_default_tools(self):
        """Register default user tools"""
        self.tools["user_check_device_lights"] = CheckDeviceLightsTool(self.env_state)
        self.tools["user_press_button"] = PressButtonTool(self.env_state)
        self.tools["user_check_device_status"] = CheckDeviceStatusTool(self.env_state)

    def register_tool(self, tool: Tool):
        """Register a custom user tool

        Args:
            tool: Tool instance to register
        """
        self.tools[tool.name] = tool

    def get_schemas(
        self, enabled_tools: list[str] | None = None, sanitize: bool = True
    ) -> list[dict[str, Any]]:
        """Get schemas for user tools

        Args:
            enabled_tools: Optional list of tool names to enable (all if None)
            sanitize: If True, sanitize property names to match LLM API requirements
                     (pattern: ^[a-zA-Z0-9_.-]+$). Default is True.

        Returns:
            List of tool schemas in OpenAI function format
        """
        if enabled_tools is None:
            enabled_tools = list(self.tools.keys())

        schemas = [self.tools[name].get_schema() for name in enabled_tools if name in self.tools]
        if sanitize:
            schemas = [sanitize_tool_schema(s) for s in schemas]
        return schemas

    def execute(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute a user tool

        Args:
            tool_name: Name of the tool to execute
            arguments: Tool parameters as dict

        Returns:
            ToolResult with success status and output/error
        """
        import time

        start_time = time.time()

        if tool_name not in self.tools:
            result = ToolResult(
                success=False, output="", error=f"User tool '{tool_name}' not found"
            )
        else:
            try:
                result = self.tools[tool_name].execute(**arguments)
            except Exception as e:
                result = ToolResult(
                    success=False, output="", error=f"User tool execution failed: {str(e)}"
                )

        # Log the tool call
        self.tool_logs.append(
            {
                "tool": tool_name,
                "arguments": arguments,
                "success": result.success,
                "duration_s": time.time() - start_time,
                "error": result.error,
                "timestamp": time.time(),
            }
        )

        return result

    def get_logs(self) -> list[dict[str, Any]]:
        """Get tool execution logs"""
        return self.tool_logs

    def reset(self):
        """Reset tool logs"""
        self.tool_logs = []

    def has_tool(self, tool_name: str) -> bool:
        """Check if a user tool is available"""
        return tool_name in self.tools
