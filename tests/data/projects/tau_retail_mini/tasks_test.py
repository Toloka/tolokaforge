"""Test tasks for tau_retail_mini.

This mimics the structure of tau-bench tasks_test.py files.
Contains 3 simple tasks for testing TauAdapter.
"""

# Use direct import since TauAdapter adds env_path to sys.path
from types_local import Action, Task

TASKS_TEST = [
    Task(
        task_id="test_001",
        annotator="test",
        user_id="john_doe_1234",
        instruction="You are John Doe. You want to check your order status for order #W1234567.",
        actions=[
            Action(
                name="find_user_id_by_name_zip",
                kwargs={"first_name": "John", "last_name": "Doe", "zip": "10001"},
            ),
            Action(name="get_order_details", kwargs={"order_id": "#W1234567"}),
        ],
        outputs=[],
    ),
    Task(
        task_id="test_002",
        annotator="test",
        user_id="jane_smith_5678",
        instruction="You are Jane Smith in 90210. You want to return the laptop from your order #W7654321 because it was damaged.",
        actions=[
            Action(
                name="find_user_id_by_name_zip",
                kwargs={"first_name": "Jane", "last_name": "Smith", "zip": "90210"},
            ),
            Action(name="get_order_details", kwargs={"order_id": "#W7654321"}),
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W7654321",
                    "item_ids": ["item_laptop_123"],
                    "payment_method_id": "credit_card_456",
                },
            ),
        ],
        outputs=[],
    ),
    Task(
        annotator="test",
        user_id="bob_wilson_9999",
        instruction="You are Bob Wilson. Your email is bob.wilson@example.com. You want to know the total amount of your pending orders.",
        actions=[
            Action(
                name="find_user_id_by_email",
                kwargs={"email": "bob.wilson@example.com"},
            ),
            Action(name="get_user_details", kwargs={"user_id": "bob_wilson_9999"}),
        ],
        outputs=["$250.00"],
    ),
]
