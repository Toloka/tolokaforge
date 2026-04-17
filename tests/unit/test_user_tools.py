"""Tests for user-side tools (dual-control)"""

import pytest

from tolokaforge.tools.user_tools import (
    CheckDeviceLightsTool,
    CheckDeviceStatusTool,
    PressButtonTool,
    UserToolExecutor,
)

pytestmark = pytest.mark.unit


@pytest.mark.unit
class TestCheckDeviceLightsTool:
    """Test user_check_device_lights tool"""

    def test_check_lights_device_not_found(self, mock_env_state):
        """Test checking lights on non-existent device"""
        env_state = mock_env_state
        env_state.db_state = {"devices": {}}

        tool = CheckDeviceLightsTool(env_state)
        result = tool.execute(device_id="modem")

        assert result.success is False
        assert "not found" in result.error

    def test_check_lights_no_db(self, mock_env_state):
        """Test checking lights with no database state"""
        env_state = mock_env_state

        tool = CheckDeviceLightsTool(env_state)
        result = tool.execute(device_id="modem")

        assert result.success is False
        assert "No database state" in result.error


@pytest.mark.unit
class TestPressButtonTool:
    """Test user_press_button tool"""

    def test_press_button_not_found(self, mock_env_state):
        """Test pressing non-existent button"""
        env_state = mock_env_state
        env_state.db_state = {
            "devices": {"modem": {"buttons": {"power": {"effect": "power_cycle"}}}}
        }

        tool = PressButtonTool(env_state)
        result = tool.execute(device_id="modem", button_name="nonexistent")

        assert result.success is False
        assert "not found" in result.error
        assert "Available buttons: power" in result.error


@pytest.mark.unit
class TestCheckDeviceStatusTool:
    """Test user_check_device_status tool"""

    def test_check_status_success(self, mock_env_state):
        """Test checking device status successfully"""
        env_state = mock_env_state
        env_state.db_state = {
            "devices": {
                "set_top_box": {
                    "powered_on": True,
                    "display": {"text": "CH 105"},
                    "connectivity": {"hdmi": "connected", "coax": "connected"},
                    "status": {"signal_strength": "good"},
                }
            }
        }

        tool = CheckDeviceStatusTool(env_state)
        result = tool.execute(device_id="set_top_box")

        assert result.success is True
        assert "Device: set_top_box" in result.output
        assert "Power: ON" in result.output
        assert "Display: CH 105" in result.output
        assert "HDMI: connected" in result.output
        assert "signal_strength: good" in result.output

    def test_check_status_powered_off(self, mock_env_state):
        """Test checking status of powered off device"""
        env_state = mock_env_state
        env_state.db_state = {"devices": {"modem": {"powered_on": False}}}

        tool = CheckDeviceStatusTool(env_state)
        result = tool.execute(device_id="modem")

        assert result.success is True
        assert "Power: OFF" in result.output


@pytest.mark.unit
class TestUserToolExecutor:
    """Test UserToolExecutor"""

    def test_default_tools_registered(self, mock_env_state):
        """Test default user tools are registered"""
        env_state = mock_env_state
        executor = UserToolExecutor(env_state)

        assert executor.has_tool("user_check_device_lights")
        assert executor.has_tool("user_press_button")
        assert executor.has_tool("user_check_device_status")

    def test_execute_tool(self, mock_env_state):
        """Test executing user tool via executor"""
        env_state = mock_env_state
        env_state.db_state = {
            "devices": {
                "modem": {"powered_on": True, "buttons": {"power": {"effect": "power_cycle"}}}
            }
        }

        executor = UserToolExecutor(env_state)
        result = executor.execute(
            "user_press_button", {"device_id": "modem", "button_name": "power"}
        )

        assert result.success is True
        assert "Pressed power button" in result.output

    def test_execute_nonexistent_tool(self, mock_env_state):
        """Test executing non-existent tool"""
        env_state = mock_env_state
        executor = UserToolExecutor(env_state)

        result = executor.execute("nonexistent_tool", {"device_id": "modem"})

        assert result.success is False
        assert "not found" in result.error
