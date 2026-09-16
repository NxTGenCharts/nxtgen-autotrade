import os
import tempfile
import unittest

from tests.mock_deriv_server import MockDerivServer, VALID_TOKEN


class DerivAdapterTestCase(unittest.TestCase):
    def setUp(self):
        import models
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        models.DB_PATH = self.tmp.name
        import importlib
        importlib.reload(models)
        models.DB_PATH = self.tmp.name
        models.init_db()
        self.models = models
        self.db = models.get_db()

        # minimal fixtures: a user, a Deriv broker_account row, a bot row
        self.db.execute("INSERT INTO users (email,password_hash,salt,display_name) VALUES ('d@t.com','x','y','D')")
        self.user_id = self.db.execute("SELECT id FROM users WHERE email='d@t.com'").fetchone()["id"]
        self.db.execute(
            """INSERT INTO broker_accounts
               (user_id, broker_id, broker_name, account_type, label, balance, equity, leverage, currency, status)
               VALUES (?, 'deriv', 'Deriv', 'demo', 'Deriv Demo', 10000, 10000, 50, 'USD', 'connected')""",
            (self.user_id,),
        )
        self.account_id = self.db.execute("SELECT id FROM broker_accounts").fetchone()["id"]
        self.db.execute(
            """INSERT INTO bots (user_id, account_id, name, strategy, symbol, risk_profile, capital, config_json, state, peak_equity)
               VALUES (?, ?, 'T', 'dca_trend', 'R_100', 'balanced', 500, '{}', 'running', 10000)""",
            (self.user_id, self.account_id),
        )
        self.bot_id = self.db.execute("SELECT id FROM bots").fetchone()["id"]
        self.db.commit()

        from brokers.deriv_adapter import DerivAdapter
        self.adapter = DerivAdapter()
        self.server = MockDerivServer()
        self.server.start()

    def tearDown(self):
        self.server.stop()
        os.unlink(self.tmp.name)

    def _connect(self, token=VALID_TOKEN):
        return self.adapter.connect_account({
            "app_id": "1089", "api_token": token, "account_id": self.account_id, "ws_url": self.server.url,
        })

    def test_connect_with_no_credentials_raises_not_connected(self):
        from brokers.base import NotConnectedError
        with self.assertRaises(NotConnectedError):
            self.adapter.connect_account({})

    def test_connect_bad_token_raises(self):
        from brokers.base import BrokerError
        with self.assertRaises(BrokerError):
            self._connect(token="WRONG")

    def test_full_surface_against_mock_server(self):
        info = self._connect()
        self.assertEqual(info["balance"], 10000.0)

        acc_info = self.adapter.get_account_info(self.account_id)
        self.assertEqual(acc_info["balance"], 10000.0)
        self.assertEqual(acc_info["used_margin"], 0.0)  # no open positions yet

        symbols = self.adapter.get_symbols(self.account_id)
        self.assertEqual(len(symbols), 3)
        self.assertIn("R_100", [s["broker_symbol"] for s in symbols])

        price = self.adapter.get_price(self.account_id, "R_100")
        self.assertIn("bid", price)

        order = self.adapter.place_order(self.account_id, "R_100", "buy", 10, bot_id=self.bot_id)
        self.assertIn("position_id", order)
        self.assertIn("broker_ref", order)

        # used_margin should now reflect the open stake
        acc_info2 = self.adapter.get_account_info(self.account_id)
        self.assertEqual(acc_info2["used_margin"], 10.0)

        positions = self.adapter.get_positions(self.account_id)
        self.assertEqual(len(positions), 1)

        closed = self.adapter.close_position(self.account_id, order["position_id"])
        self.assertIn("pl", closed)

        # margin released after close
        acc_info3 = self.adapter.get_account_info(self.account_id)
        self.assertEqual(acc_info3["used_margin"], 0.0)

        history = self.adapter.get_trade_history(self.account_id)
        self.assertEqual(len(history), 1)

        self.adapter.disconnect_account(self.account_id)

    def test_using_unconnected_account_raises(self):
        from brokers.base import NotConnectedError
        with self.assertRaises(NotConnectedError):
            self.adapter.get_account_info("never-connected")

    def test_place_order_without_bot_id_rejected(self):
        self._connect()
        from brokers.base import BrokerError
        with self.assertRaises(BrokerError):
            self.adapter.place_order(self.account_id, "R_100", "buy", 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
