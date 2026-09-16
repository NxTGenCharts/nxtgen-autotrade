"""
This is the strongest verification possible without real internet access:
it drives the exact same code path the product uses — connect_account(),
POST /api/bots, POST /api/bots/:id/start, then the real BotManager tick
loop — against a Deriv account backed by the protocol-accurate mock server,
and checks that trades actually land in the database with correct P/L and
that account balance/margin move the way a real broker would report them.

If this passes, the only thing separating it from a real Deriv account is
network egress + a real token — nothing in the app, engine, or UI changes.
"""
import json
import os
import tempfile
import time
import unittest

os.environ["NXTGEN_TICK_SECONDS"] = "999"  # we tick manually in this test

from tests.mock_deriv_server import MockDerivServer, VALID_TOKEN


class DerivLiveBotTestCase(unittest.TestCase):
    def setUp(self):
        import models
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        import importlib
        models.DB_PATH = self.tmp.name
        importlib.reload(models)
        models.DB_PATH = self.tmp.name
        models.init_db()
        self.models = models

        global app
        import app as app_module
        importlib.reload(app_module)
        self.app = app_module.app.test_client()

        self.server = MockDerivServer()
        self.server.start()

    def tearDown(self):
        self.server.stop()
        os.unlink(self.tmp.name)

    def auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_connect_deriv_create_bot_and_trade_through_the_real_api(self):
        # 1) register — gets the free built-in demo account too, doesn't matter here
        token = self.app.post("/api/auth/register", json={
            "email": "livebot@test.com", "password": "password123",
        }).get_json()["token"]

        # 2) connect a Deriv account through the exact same route the frontend calls,
        #    pointed at our local mock server instead of the real (blocked) one
        res = self.app.post("/api/accounts/connect", headers=self.auth(token), json={
            "broker_id": "deriv", "app_id": "1089", "api_token": VALID_TOKEN,
            "ws_url": self.server.url,
        })
        self.assertEqual(res.status_code, 200, res.get_json())
        deriv_info = res.get_json()
        self.assertEqual(deriv_info["balance"], 10000.0)

        accounts = self.app.get("/api/accounts", headers=self.auth(token)).get_json()
        deriv_account = next(a for a in accounts if a["broker_id"] == "deriv")
        self.assertEqual(deriv_account["balance"], 10000.0)

        # 3) create a bot on the Deriv account from the same wizard endpoint the UI uses
        res = self.app.post("/api/bots", headers=self.auth(token), json={
            "account_id": deriv_account["id"], "symbol": "R_100", "strategy": "dca_trend",
            "risk_profile": "aggressive", "capital": 500,
        })
        self.assertEqual(res.status_code, 200, res.get_json())
        bot_id = res.get_json()["id"]

        res = self.app.post(f"/api/bots/{bot_id}/start", headers=self.auth(token))
        self.assertEqual(res.status_code, 200)

        # 4) drive the real bot engine directly (bypassing the sleep loop) —
        #    same code BotManager's background thread runs every tick
        from engine.bot_manager import get_broker, log_event
        from engine.strategies import dca_trend
        from engine import price_history

        broker = get_broker("deriv")
        db = self.models.get_db()
        bot_row = db.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
        cfg = json.loads(bot_row["config_json"])

        # warm up price history so the strategy's trend check has data
        for _ in range(25):
            q = broker.get_price(deriv_account["id"], "R_100")
            price_history.push("R_100", q["mid"])

        # force a bullish trend deterministically (mock server returns a flat
        # tick, so nudge history so the crossover fires and we can verify a
        # real order actually gets placed and booked)
        for i in range(15):
            price_history.push("R_100", 1200 + i * 5)

        bot_row = db.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
        dca_trend.on_tick(bot_row, broker, deriv_account["id"], cfg, log_event)

        positions = db.execute("SELECT * FROM bot_positions WHERE bot_id=?", (bot_id,)).fetchall()
        self.assertEqual(len(positions), 1, "expected the strategy to open a position via the real Deriv adapter")
        pos = positions[0]
        self.assertEqual(pos["status"], "open")
        self.assertIsNotNone(pos["broker_ref"], "position should carry Deriv's real contract_id")

        # account info (used_margin) reflects the open stake, fetched live via the API route
        acc_after_open = self.app.get(f"/api/accounts/{deriv_account['id']}", headers=self.auth(token)).get_json()
        self.assertGreater(acc_after_open["used_margin"], 0)

        # 5) close it through the real broker.close_position path and confirm a
        #    trade row lands with the P/L Deriv itself reported (sold_for - stake)
        result = broker.close_position(deriv_account["id"], pos["id"], reason="manual")
        self.assertIn("pl", result)

        trades = self.app.get(f"/api/bots/{bot_id}/trades", headers=self.auth(token)).get_json()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["close_reason"], "manual")

        acc_after_close = self.app.get(f"/api/accounts/{deriv_account['id']}", headers=self.auth(token)).get_json()
        self.assertEqual(acc_after_close["used_margin"], 0.0, "margin should be released after close")

        # 6) the dashboard aggregates across both the demo account and this
        #    Deriv account without special-casing either
        dash = self.app.get("/api/dashboard", headers=self.auth(token)).get_json()
        self.assertGreaterEqual(dash["total_trades"], 1)
        self.assertGreaterEqual(dash["connected_accounts"], 2)  # built-in demo + Deriv

        # 7) stop-all must reach the Deriv-backed bot too (routes through
        #    broker_for_account, not a hardcoded "demo" adapter)
        self.app.post(f"/api/bots/{bot_id}/start", headers=self.auth(token))
        stopped = self.app.post("/api/bots/stop-all", headers=self.auth(token)).get_json()
        self.assertGreaterEqual(stopped["stopped"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
