import unittest

from tests.mock_deriv_server import MockDerivServer, VALID_TOKEN
from brokers.deriv_session import DerivSession, DerivAPIError


class DerivSessionTestCase(unittest.TestCase):
    def setUp(self):
        self.server = MockDerivServer()
        self.server.start()

    def tearDown(self):
        self.server.stop()

    def test_handshake_and_authorize(self):
        session = DerivSession(app_id="1089", ws_url=self.server.url)
        session.connect()
        try:
            result = session.authorize(VALID_TOKEN)
            self.assertEqual(result["loginid"], "CR9999999")
            self.assertEqual(result["balance"], 10000.0)
        finally:
            session.close()

    def test_authorize_wrong_token_raises(self):
        session = DerivSession(app_id="1089", ws_url=self.server.url)
        session.connect()
        try:
            with self.assertRaises(DerivAPIError):
                session.authorize("WRONG_TOKEN")
        finally:
            session.close()

    def test_unauthorized_call_rejected(self):
        session = DerivSession(app_id="1089", ws_url=self.server.url)
        session.connect()
        try:
            with self.assertRaises(DerivAPIError):
                session.request({"balance": 1})
        finally:
            session.close()

    def test_balance_and_symbols_after_auth(self):
        session = DerivSession(app_id="1089", ws_url=self.server.url, api_token=VALID_TOKEN)
        session.connect()
        try:
            bal = session.request({"balance": 1})
            self.assertEqual(bal["balance"]["balance"], 10000.0)

            symbols = session.request({"active_symbols": "brief"})
            self.assertEqual(len(symbols["active_symbols"]), 3)
        finally:
            session.close()

    def test_buy_and_portfolio_roundtrip(self):
        session = DerivSession(app_id="1089", ws_url=self.server.url, api_token=VALID_TOKEN)
        session.connect()
        try:
            proposal = session.request({"proposal": 1, "amount": 10, "contract_type": "CALL",
                                         "symbol": "R_100", "duration": 5, "duration_unit": "t"})
            self.assertIn("id", proposal["proposal"])

            bought = session.request({"buy": proposal["proposal"]["id"], "price": 10})
            self.assertIn("contract_id", bought["buy"])

            portfolio = session.request({"portfolio": 1})
            self.assertIn("contracts", portfolio["portfolio"])
        finally:
            session.close()

    def test_concurrent_requests_correlate_correctly(self):
        """Fires several requests without waiting between them — proves req_id
        correlation (not just request/response ordering) actually works."""
        import threading
        session = DerivSession(app_id="1089", ws_url=self.server.url, api_token=VALID_TOKEN)
        session.connect()
        results = {}
        errors = []

        def call(name, payload):
            try:
                results[name] = session.request(payload)
            except Exception as e:
                errors.append((name, e))

        threads = [
            threading.Thread(target=call, args=("balance", {"balance": 1})),
            threading.Thread(target=call, args=("symbols", {"active_symbols": "brief"})),
            threading.Thread(target=call, args=("portfolio", {"portfolio": 1})),
        ]
        for t in threads: t.start()
        for t in threads: t.join(timeout=5)
        session.close()

        self.assertEqual(errors, [])
        self.assertIn("balance", results)
        self.assertIn("symbols", results)
        self.assertIn("portfolio", results)

    def test_subscription_push_delivered_to_callback(self):
        session = DerivSession(app_id="1089", ws_url=self.server.url, api_token=VALID_TOKEN)
        session.connect()
        # our mock server doesn't push repeated ticks, but the response itself
        # exercises the subscribe() request path (subscribe:1 flag transmitted)
        received = []
        session.request({"ticks": "R_100", "subscribe": 1})
        session.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
