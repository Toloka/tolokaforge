"""Target test: ``GET /orders/{order_id}/summary`` — the endpoint to add.

Fails against the pristine source (404, route missing) and passes once the agent
adds an endpoint that joins an order onto its customer and returns the summary
shape asserted below. Runs in-process against the real app-db.
"""

import unittest

from app import app
from starlette.testclient import TestClient


class OrderSummaryTest(unittest.TestCase):
    def test_summary_joins_order_and_customer(self) -> None:
        with TestClient(app) as client:
            resp = client.get("/orders/5001/summary")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["order_id"], 5001)
        self.assertEqual(body["product"], "Widget crate")
        self.assertEqual(body["status"], "shipped")

        customer = body["customer"]
        self.assertEqual(customer["customer_id"], 1)
        self.assertEqual(customer["name"], "Acme Industries")
        self.assertEqual(customer["tier"], "gold")

    def test_summary_unknown_order_is_404(self) -> None:
        with TestClient(app) as client:
            resp = client.get("/orders/9999/summary")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
