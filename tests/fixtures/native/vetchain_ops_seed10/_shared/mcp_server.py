"""BluePine Veterinary Partners – vetchain_ops MCP server.

Policy traceability:
  pol_dea_registration_required  – verify_dea_registration, dispense_controlled_substance
  pol_dea_registration_expired   – verify_dea_registration, dispense_controlled_substance
  pol_controlled_dispense_log    – log_controlled_dispense, dispense_controlled_substance
  pol_schedule_ii_witness_required – dispense_controlled_substance
  pol_staff_suspended_blocked    – dispense_controlled_substance
"""

from tolokaforge.core.tools_interface import create_server

mcp, registry, TOOLS = create_server(__file__, "vetchain-ops")

from tools import register_all  # noqa: E402

register_all(registry)

if __name__ == "__main__":
    mcp.run(transport="stdio")
