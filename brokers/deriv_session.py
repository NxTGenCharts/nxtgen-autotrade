"""
DerivSession — the real Deriv WebSocket protocol on top of brokers/ws_client.py.

Protocol basics (https://developers.deriv.com/docs/websockets):
  - one WS connection to wss://ws.derivws.com/websockets/v3?app_id=<id>
  - every request is a JSON object; you may attach "req_id": <int> and Deriv
    echoes it back on the matching response, which is how we correlate
    concurrent in-flight requests on a single connection
  - {"authorize": "<token>"} must succeed before account-scoped calls
  - streaming calls (ticks, balance, transaction) take "subscribe": 1 and
    keep pushing messages tagged with a "subscription": {"id": ...} object
    until you call {"forget": id} or {"forget_all": [...]}

This class owns one background reader thread that dispatches every incoming
frame either to whoever is blocked waiting on that req_id (`request()`), or
to a registered streaming callback (`subscribe()`).
"""
import itertools
import json
import threading
import time

from brokers.ws_client import WebSocketClient, WebSocketError

DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3"


class DerivAPIError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


class DerivSession:
    def __init__(self, app_id, api_token=None, ws_url=None, request_timeout=10):
        self.app_id = app_id
        self.api_token = api_token
        self.ws_url = ws_url or f"{DERIV_WS_URL}?app_id={app_id}"
        self.request_timeout = request_timeout

        self._ws = None
        self._reader_thread = None
        self._running = False
        self._req_id_counter = itertools.count(1)

        self._pending = {}       # req_id -> {"event": Event, "response": dict}
        self._pending_lock = threading.Lock()
        self._subscriptions = {}  # subscription_id -> callback(payload)
        self._sub_lock = threading.Lock()

        self.authorized_account = None

    # --------------------------------------------------------------- open
    def connect(self):
        self._ws = WebSocketClient(self.ws_url)
        self._ws.connect()
        self._running = True
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
        if self.api_token:
            self.authorize(self.api_token)

    def close(self):
        self._running = False
        if self._ws:
            self._ws.close()

    # ------------------------------------------------------------- reader
    def _read_loop(self):
        while self._running:
            try:
                text = self._ws.recv()
            except WebSocketError:
                self._running = False
                self._fail_all_pending("Connection closed")
                return
            except OSError:
                self._running = False
                self._fail_all_pending("Socket closed")
                return
            if text is None:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            self._dispatch(msg)

    def _dispatch(self, msg):
        req_id = msg.get("req_id")
        sub = msg.get("subscription")
        if sub and sub.get("id"):
            with self._sub_lock:
                cb = self._subscriptions.get(sub["id"])
            if cb:
                cb(msg)
                return
        if req_id is not None:
            with self._pending_lock:
                entry = self._pending.get(req_id)
            if entry:
                entry["response"] = msg
                entry["event"].set()

    def _fail_all_pending(self, reason):
        with self._pending_lock:
            for entry in self._pending.values():
                entry["response"] = {"error": {"code": "ConnectionClosed", "message": reason}}
                entry["event"].set()

    # ------------------------------------------------------------ request
    def request(self, payload, timeout=None):
        if not self._running:
            raise DerivAPIError("NotConnected", "WebSocket session is not open")
        req_id = next(self._req_id_counter)
        payload = dict(payload, req_id=req_id)
        event = threading.Event()
        with self._pending_lock:
            self._pending[req_id] = {"event": event, "response": None}
        self._ws.send_text(json.dumps(payload))

        if not event.wait(timeout or self.request_timeout):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise DerivAPIError("Timeout", f"No response for {list(payload.keys())[0]} within timeout")

        with self._pending_lock:
            entry = self._pending.pop(req_id, None)
        response = entry["response"] if entry else None
        if response and "error" in response:
            err = response["error"]
            raise DerivAPIError(err.get("code", "Unknown"), err.get("message", "Deriv API error"))
        return response

    def subscribe(self, payload, callback, timeout=None):
        """Sends a request with subscribe:1; registers callback for pushes;
        returns the initial response (which carries the subscription id)."""
        response = self.request(dict(payload, subscribe=1), timeout=timeout)
        sub_id = (response.get("subscription") or {}).get("id")
        if sub_id:
            with self._sub_lock:
                self._subscriptions[sub_id] = callback
        return response

    def unsubscribe(self, sub_id):
        with self._sub_lock:
            self._subscriptions.pop(sub_id, None)
        try:
            self.request({"forget": sub_id})
        except DerivAPIError:
            pass

    # ------------------------------------------------------------- calls
    def authorize(self, token):
        response = self.request({"authorize": token})
        self.authorized_account = response.get("authorize")
        return self.authorized_account
