"""Tool-registry tools (API-Bank-derived, native stubs).

Lifts the meta-tool FLOW from the public API-Bank benchmark: the agent calls
`search_tools` to DISCOVER which tool fits a request, reads the returned
descriptor, then CALLS that tool - threading an access token from
`get_access_token` (lifecycle), often needing ids from 2+ earlier calls
(fan-in). The large catalog stresses schema serialisation at volume. Stubs only
(echo); no policy, no real credentials, no business logic.

No ``from __future__ import annotations`` here: FastMCP must resolve the model
types as real objects to derive the schemas.
"""

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field

from models import DeviceSpec

from tolokaforge.core.tools_interface import DomainToolRegistry


def register(registry: DomainToolRegistry) -> None:
    @registry.tool(
        "Meta-tool: search the registry for tools matching `keywords`. Returns a "
        "list of tool descriptors (name + what it does + its parameters). Call "
        "this FIRST when you do not know which tool fits, then call the tool it "
        "names."
    )
    def search_tools(
        data: dict,
        keywords: Annotated[
            str, Field(description="Free-text keywords to match tools against.", examples=["schedule meeting"])
        ],
    ) -> dict:
        catalog = [
            {"name": "add_meeting", "description": "Schedule a calendar meeting.",
             "parameters": ["token", "topic", "start_time", "attendees"]},
            {"name": "add_reminder", "description": "Create a reminder.",
             "parameters": ["token", "content", "remind_at"]},
            {"name": "transfer_funds", "description": "Move money between two accounts.",
             "parameters": ["token", "from_account", "to_account", "amount"]},
            {"name": "register_devices", "description": "Register devices on the account.",
             "parameters": ["token", "devices"]},
        ]
        return {"ok": True, "keywords": keywords, "matches": catalog}

    @registry.tool(
        "Authenticate and obtain an access token. Returns the token that every "
        "account-modifying tool requires."
    )
    def get_access_token(
        data: dict,
        username: Annotated[str, Field(description="Username.", examples=["alice"])],
        password: Annotated[str, Field(description="Password.")],
    ) -> dict:
        return {"ok": True, "token": "TOK-9C2E"}

    @registry.tool(
        "Schedule a calendar meeting. Requires the access token plus the meeting "
        "details. `attendees` is a list of names."
    )
    def add_meeting(
        data: dict,
        token: Annotated[str, Field(description="Access token from get_access_token.", examples=["TOK-9C2E"])],
        topic: Annotated[str, Field(description="Meeting topic.")],
        start_time: Annotated[
            str, Field(description="Start time, ISO-8601.", examples=["2026-07-02T15:00:00Z"])
        ],
        attendees: Annotated[list[str], Field(min_length=1, description="Attendee names.")],
    ) -> dict:
        return {"ok": True, "topic": topic, "attendees": list(attendees)}

    @registry.tool("Create a reminder. Requires the access token.")
    def add_reminder(
        data: dict,
        token: Annotated[str, Field(description="Access token.", examples=["TOK-9C2E"])],
        content: Annotated[str, Field(description="Reminder text.")],
        remind_at: Annotated[
            str, Field(description="When to remind, ISO-8601.", examples=["2026-07-03T09:00:00Z"])
        ],
    ) -> dict:
        return {"ok": True, "content": content, "remind_at": remind_at}

    @registry.tool("Set a one-off alarm. Requires the access token.")
    def add_alarm(
        data: dict,
        token: Annotated[str, Field(description="Access token.", examples=["TOK-9C2E"])],
        alarm_time: Annotated[str, Field(description="Alarm time HH:MM (24h).", examples=["07:30"])],
    ) -> dict:
        return {"ok": True, "alarm_time": alarm_time}

    @registry.tool(
        "Book a meeting room. Requires the access token. `capacity` must be "
        "between 1 and 50."
    )
    def book_room(
        data: dict,
        token: Annotated[str, Field(description="Access token.", examples=["TOK-9C2E"])],
        room_id: Annotated[str, Field(description="Room id.", examples=["ROOM-3"])],
        date: Annotated[str, Field(description="Date, YYYY-MM-DD.", examples=["2026-07-02"])],
        capacity: Annotated[int, Field(ge=1, le=50, description="People to seat (1-50).")],
    ) -> dict:
        return {"ok": True, "room_id": room_id, "date": date, "capacity": capacity}

    @registry.tool("Read an account balance. Requires the access token.")
    def query_balance(
        data: dict,
        token: Annotated[str, Field(description="Access token.", examples=["TOK-9C2E"])],
        account_id: Annotated[str, Field(description="Account id.", examples=["A-1"])],
    ) -> dict:
        return {"ok": True, "account_id": account_id, "balance": "1000.00"}

    @registry.tool(
        "Transfer money between two accounts. Requires the access token and BOTH "
        "account ids."
    )
    def transfer_funds(
        data: dict,
        token: Annotated[str, Field(description="Access token.", examples=["TOK-9C2E"])],
        from_account: Annotated[str, Field(description="Source account id.", examples=["A-1"])],
        to_account: Annotated[str, Field(description="Destination account id.", examples=["A-2"])],
        amount: Annotated[Decimal, Field(gt=0, description="Amount to transfer.", examples=["40.00"])],
    ) -> dict:
        return {"ok": True, "from_account": from_account, "to_account": to_account, "amount": str(amount)}

    @registry.tool(
        "Register devices on the account. Requires the access token. `devices` is "
        "a map of a label to a device entry."
    )
    def register_devices(
        data: dict,
        token: Annotated[str, Field(description="Access token.", examples=["TOK-9C2E"])],
        devices: Annotated[
            dict[str, DeviceSpec],
            Field(description="Map label -> device entry.", min_length=1),
        ],
    ) -> dict:
        keys = list(devices.keys()) if isinstance(devices, dict) else []
        return {"ok": True, "labels": keys}

    @registry.tool(
        "Set free-form account preferences. Requires the access token. `prefs` is "
        "an open object: any string keys mapping to any values."
    )
    def set_preferences(
        data: dict,
        token: Annotated[str, Field(description="Access token.", examples=["TOK-9C2E"])],
        prefs: Annotated[dict[str, Any], Field(description="Open preferences object.")],
    ) -> dict:
        keys = list(prefs.keys()) if isinstance(prefs, dict) else []
        return {"ok": True, "pref_keys": keys}

    @registry.tool("Search for hotels in a city for a date range. No token required.")
    def search_hotels(
        data: dict,
        city: Annotated[str, Field(description="City.", examples=["Riverton"])],
        check_in: Annotated[str, Field(description="Check-in date YYYY-MM-DD.", examples=["2026-07-02"])],
        check_out: Annotated[str, Field(description="Check-out date YYYY-MM-DD.", examples=["2026-07-05"])],
    ) -> dict:
        return {"ok": True, "city": city, "hotels": ["HOTEL-1", "HOTEL-2"]}

    @registry.tool("Cancel a booking. Requires the access token.")
    def cancel_booking(
        data: dict,
        token: Annotated[str, Field(description="Access token.", examples=["TOK-9C2E"])],
        booking_id: Annotated[str, Field(description="Booking id.", examples=["BK-5"])],
    ) -> dict:
        return {"ok": True, "booking_id": booking_id, "cancelled": True}

    @registry.tool("Get registry/service health. Takes no parameters.")
    def get_service_health(data: dict) -> dict:
        return {"ok": True, "status": "healthy"}
