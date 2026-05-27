"""Minimal tools for tau_retail_mini test project."""

from typing import Any


class Tool:
    """Base class for Tau-style tools."""

    @classmethod
    def get_info(cls) -> dict[str, Any]:
        raise NotImplementedError

    @classmethod
    def invoke(cls, data: dict[str, Any], **kwargs) -> Any:
        raise NotImplementedError


class LookupUser(Tool):
    """Lookup user by email."""

    @classmethod
    def get_info(cls) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "lookup_user",
                "description": "Look up a user by their email address",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "email": {"type": "string", "description": "The user's email address"}
                    },
                    "required": ["email"],
                },
            },
        }

    @classmethod
    def invoke(cls, data: dict[str, Any], email: str) -> dict[str, Any]:
        """Look up user by email."""
        users = data.get("users", [])
        for user in users:
            if user.get("email") == email:
                return {"status": "found", "user": user}
        return {"status": "not_found", "message": f"User with email {email} not found"}


class GetUserOrders(Tool):
    """Get orders for a user."""

    @classmethod
    def get_info(cls) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "get_user_orders",
                "description": "Get all orders for a user by their user ID",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string", "description": "The user's ID"},
                        "status": {
                            "type": "string",
                            "description": "Filter by order status (pending, delivered, cancelled)",
                            "enum": ["pending", "delivered", "cancelled"],
                        },
                    },
                    "required": ["user_id"],
                },
            },
        }

    @classmethod
    def invoke(cls, data: dict[str, Any], user_id: str, status: str = None) -> dict[str, Any]:
        """Get orders for user."""
        orders = data.get("orders", [])
        user_orders = [o for o in orders if o.get("user_id") == user_id]

        if status:
            user_orders = [o for o in user_orders if o.get("status") == status]

        return {"orders": user_orders, "count": len(user_orders)}


class SendMessage(Tool):
    """Send a message to the user."""

    @classmethod
    def get_info(cls) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "send_message",
                "description": "Send a message response to the user",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "The message to send to the user",
                        }
                    },
                    "required": ["message"],
                },
            },
        }

    @classmethod
    def invoke(cls, data: dict[str, Any], message: str) -> dict[str, Any]:
        """Send message - just returns the message for confirmation."""
        return {"status": "sent", "message": message}


# List of all tools - TauAdapter looks for this
ALL_TOOLS = [LookupUser, GetUserOrders, SendMessage]
