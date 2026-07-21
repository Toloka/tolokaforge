"""Regression guard for the three existing orders endpoints.

Runs in-process against the real app-db via Starlette's TestClient (used as a
context manager so the FastAPI lifespan opens the asyncpg pool). These must keep
passing while the summary endpoint is added.
"""

import unittest

from app import app
from starlette.testclient import TestClient


class ExistingEndpointsTest(unittest.TestCase):
    def test_list_orders(self) -> None:
        with TestClient(app) as client:
            resp = client.get("/orders")
        self.assertEqual(resp.status_code, 200)
        orders = resp.json()
        self.assertEqual([o["order_id"] for o in orders], [5001, 5002, 5003, 5004])

    def test_get_order(self) -> None:
        with TestClient(app) as client:
            resp = client.get("/orders/5001")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["product"], "Widget crate")
        self.assertEqual(body["status"], "shipped")

    def test_get_customer(self) -> None:
        with TestClient(app) as client:
            resp = client.get("/customers/1")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["name"], "Acme Industries")
        self.assertEqual(body["tier"], "gold")


if __name__ == "__main__":
    unittest.main()
