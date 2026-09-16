"""
Run with:  python3 -m pytest test_app.py -v      (if pytest is available)
or simply: python3 test_app.py                    (plain assertions, no deps)
"""
import json
import os
import tempfile
import unittest

os.environ["NXTGEN_TICK_SECONDS"] = "999"  # keep the background loop from firing mid-test


class NxTGenTestCase(unittest.TestCase):
    def setUp(self):
        # isolate each test on its own throwaway sqlite file
        import models
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        models.DB_PATH = self.tmp.name
        import importlib
        importlib.reload(models)
        models.DB_PATH = self.tmp.name
        models.init_db()
        self.models = models

        global app
        import app as app_module
        importlib.reload(app_module)
        self.app = app_module.app.test_client()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def register(self, email="user@test.com", password="password123"):
        res = self.app.post("/api/auth/register", json={"email": email, "password": password})
        self.assertEqual(res.status_code, 200)
        return res.get_json()["token"]

    def auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    # ---------------------------------------------------------------- auth
    def test_register_and_demo_account_created(self):
        token = self.register()
        res = self.app.get("/api/accounts", headers=self.auth(token))
        accounts = res.get_json()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["broker_id"], "demo")
        self.assertEqual(accounts[0]["balance"], 10000.0)

    def test_duplicate_email_rejected(self):
        self.register("dup@test.com")
        res = self.app.post("/api/auth/register", json={"email": "dup@test.com", "password": "password123"})
        self.assertEqual(res.status_code, 409)

    def test_weak_password_rejected(self):
        res = self.app.post("/api/auth/register", json={"email": "a@test.com", "password": "123"})
        self.assertEqual(res.status_code, 400)

    def test_login_wrong_password(self):
        self.register("login@test.com", "password123")
        res = self.app.post("/api/auth/login", json={"email": "login@test.com", "password": "wrong"})
        self.assertEqual(res.status_code, 401)

    def test_protected_route_requires_auth(self):
        res = self.app.get("/api/accounts")
        self.assertEqual(res.status_code, 401)

    # ------------------------------------------------------------ bot crud
    def test_create_bot_over_balance_rejected(self):
        token = self.register()
        acct = self.app.get("/api/accounts", headers=self.auth(token)).get_json()[0]
        res = self.app.post("/api/bots", headers=self.auth(token), json={
            "account_id": acct["id"], "symbol": "XAUUSDm", "strategy": "dca_trend",
            "risk_profile": "balanced", "capital": 999999,
        })
        self.assertEqual(res.status_code, 400)

    def test_create_and_start_bot(self):
        token = self.register()
        acct = self.app.get("/api/accounts", headers=self.auth(token)).get_json()[0]
        res = self.app.post("/api/bots", headers=self.auth(token), json={
            "account_id": acct["id"], "symbol": "XAUUSDm", "strategy": "dca_trend",
            "risk_profile": "balanced", "capital": 500,
        })
        self.assertEqual(res.status_code, 200)
        bot_id = res.get_json()["id"]

        res = self.app.post(f"/api/bots/{bot_id}/start", headers=self.auth(token))
        self.assertEqual(res.status_code, 200)

        bots = self.app.get("/api/bots", headers=self.auth(token)).get_json()
        self.assertEqual(bots[0]["state"], "running")

    def test_pause_resume_stop_lifecycle(self):
        token = self.register()
        acct = self.app.get("/api/accounts", headers=self.auth(token)).get_json()[0]
        bot_id = self.app.post("/api/bots", headers=self.auth(token), json={
            "account_id": acct["id"], "symbol": "EURUSDm", "strategy": "grid_sideways",
            "risk_profile": "conservative", "capital": 200,
        }).get_json()["id"]
        self.app.post(f"/api/bots/{bot_id}/start", headers=self.auth(token))
        self.app.post(f"/api/bots/{bot_id}/pause", headers=self.auth(token))
        bots = self.app.get("/api/bots", headers=self.auth(token)).get_json()
        self.assertEqual(bots[0]["state"], "paused")
        self.app.post(f"/api/bots/{bot_id}/resume", headers=self.auth(token))
        bots = self.app.get("/api/bots", headers=self.auth(token)).get_json()
        self.assertEqual(bots[0]["state"], "running")
        self.app.post(f"/api/bots/{bot_id}/stop", headers=self.auth(token))
        bots = self.app.get("/api/bots", headers=self.auth(token)).get_json()
        self.assertEqual(bots[0]["state"], "stopped")

    def test_unknown_strategy_rejected(self):
        token = self.register()
        acct = self.app.get("/api/accounts", headers=self.auth(token)).get_json()[0]
        res = self.app.post("/api/bots", headers=self.auth(token), json={
            "account_id": acct["id"], "symbol": "XAUUSDm", "strategy": "martingale_unlimited",
            "risk_profile": "balanced", "capital": 500,
        })
        self.assertEqual(res.status_code, 400)

    # -------------------------------------------------------- risk limits
    def test_max_dca_orders_is_hard_capped(self):
        from engine.presets import resolve_config
        cfg = resolve_config("dca_trend", "aggressive", 1000)
        self.assertIn("max_dca_orders", cfg)
        self.assertLess(cfg["max_dca_orders"], 100)  # sanity: never "unlimited"
        self.assertIn("max_exposure_usd", cfg)
        self.assertLessEqual(cfg["max_exposure_usd"], cfg["capital"])

    def _make_bot_id(self, token, acct):
        return self.app.post("/api/bots", headers=self.auth(token), json={
            "account_id": acct["id"], "symbol": "XAUUSDm", "strategy": "dca_trend",
            "risk_profile": "balanced", "capital": 500,
        }).get_json()["id"]

    def test_position_sizing_respects_symbol_limits(self):
        from brokers.demo_broker import DemoBrokerAdapter
        token = self.register()
        acct = self.app.get("/api/accounts", headers=self.auth(token)).get_json()[0]
        bot_id = self._make_bot_id(token, acct)
        broker = DemoBrokerAdapter()
        info = broker.get_symbol_info(acct["id"], "XAUUSDm")
        order = broker.place_order(acct["id"], "XAUUSDm", "buy", 0.005, bot_id=bot_id)  # below volume_min
        self.assertGreaterEqual(order["volume"], info["volume_min"])

    def test_insufficient_margin_rejected(self):
        from brokers.demo_broker import DemoBrokerAdapter, BrokerError
        token = self.register()
        acct = self.app.get("/api/accounts", headers=self.auth(token)).get_json()[0]
        bot_id = self._make_bot_id(token, acct)
        broker = DemoBrokerAdapter()
        with self.assertRaises(BrokerError):
            broker.place_order(acct["id"], "XAUUSDm", "buy", 5000, bot_id=bot_id)  # absurdly oversized

    # --------------------------------------------------------- idempotency
    def test_stop_all_is_idempotent_and_scoped_to_user(self):
        token1 = self.register("u1@test.com")
        token2 = self.register("u2@test.com")
        acct1 = self.app.get("/api/accounts", headers=self.auth(token1)).get_json()[0]
        bot_id = self.app.post("/api/bots", headers=self.auth(token1), json={
            "account_id": acct1["id"], "symbol": "XAUUSDm", "strategy": "dca_trend",
            "risk_profile": "balanced", "capital": 300,
        }).get_json()["id"]
        self.app.post(f"/api/bots/{bot_id}/start", headers=self.auth(token1))

        res = self.app.post("/api/bots/stop-all", headers=self.auth(token2))
        self.assertEqual(res.get_json()["stopped"], 0)  # user 2 has no bots

        res = self.app.post("/api/bots/stop-all", headers=self.auth(token1))
        self.assertEqual(res.get_json()["stopped"], 1)
        res = self.app.post("/api/bots/stop-all", headers=self.auth(token1))
        self.assertEqual(res.get_json()["stopped"], 0)  # already stopped, no double-close

    # -------------------------------------------------------------- health
    def test_health_and_ready(self):
        self.assertEqual(self.app.get("/health").status_code, 200)
        self.assertEqual(self.app.get("/ready").status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
