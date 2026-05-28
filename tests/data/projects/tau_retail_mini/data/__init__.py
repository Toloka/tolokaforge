"""Data loading for tau_retail_mini test project."""

from typing import Any


def load_data() -> dict[str, Any]:
    """Load initial data for the Tau environment."""
    return {
        "users": [
            {
                "user_id": "user_001",
                "name": "John Doe",
                "email": "john.doe@example.com",
                "phone": "+1-555-0101",
            },
            {
                "user_id": "user_002",
                "name": "Jane Smith",
                "email": "jane.smith@example.com",
                "phone": "+1-555-0102",
            },
            {
                "user_id": "user_003",
                "name": "Bob Wilson",
                "email": "bob.wilson@example.com",
                "phone": "+1-555-0103",
            },
        ],
        "orders": [
            {
                "order_id": "order_101",
                "user_id": "user_001",
                "items": ["Widget A", "Widget B"],
                "total": 45.99,
                "status": "pending",
            },
            {
                "order_id": "order_102",
                "user_id": "user_001",
                "items": ["Gadget X"],
                "total": 89.99,
                "status": "delivered",
            },
            {
                "order_id": "order_103",
                "user_id": "user_002",
                "items": ["Widget C", "Widget D", "Widget E"],
                "total": 125.50,
                "status": "pending",
            },
            {
                "order_id": "order_104",
                "user_id": "user_003",
                "items": ["Premium Package"],
                "total": 299.99,
                "status": "pending",
            },
            {
                "order_id": "order_105",
                "user_id": "user_003",
                "items": ["Basic Set"],
                "total": 49.99,
                "status": "pending",
            },
        ],
    }
