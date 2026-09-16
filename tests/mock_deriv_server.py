"""
A tiny stdlib WebSocket SERVER that speaks just enough of Deriv's JSON
protocol to let us test brokers/ws_client.py + brokers/deriv_session.py
end-to-end over localhost. This is a test fixture, not part of the shipped
product — it exists because this sandbox has no route to the real
wss://ws.derivws.com, so it's the only way to prove the client-side
WebSocket implementation (handshake, framing, req_id correlation,
subscriptions) is actually correct rather than merely "looks right".

It implements the server half of RFC 6455 by hand (same constraint as the
client: no `websockets` package available/installable here).
"""
import base64
import hashlib
import itertools
import json
import socket
import struct
import threading
import time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
VALID_TOKEN = "TEST_TOKEN_123"


def _accept_key(client_key):
    return base64.b64encode(hashlib.sha1((client_key + GUID).encode()).digest()).decode()


def _send_frame(sock, opcode, payload: bytes):
    fin_opcode = 0x80 | opcode
    length = len(payload)
    if length <= 125:
        header = struct.pack("!BB", fin_opcode, length)
    elif length <= 0xFFFF:
        header = struct.pack("!BBH", fin_opcode, 126, length)
    else:
        header = struct.pack("!BBQ", fin_opcode, 127, length)
    sock.sendall(header + payload)  # server->client frames are unmasked


def _send_text(sock, obj):
    _send_frame(sock, 0x1, json.dumps(obj).encode())


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def _recv_frame(sock):
    b0, b1 = _recv_exact(sock, 2)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    mask_key = _recv_exact(sock, 4) if masked else None
    payload = _recv_exact(sock, length) if length else b""
    if masked:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return opcode, payload


class MockDerivServer:
    def __init__(self, host="127.0.0.1", port=0):
        self.host = host
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((host, port))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self._running = False
        self._thread = None
        self._contract_ids = itertools.count(1000)

    @property
    def url(self):
        return f"ws://{self.host}:{self.port}/websockets/v3?app_id=1089"

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        try:
            self.srv.close()
        except Exception:
            pass

    def _accept_loop(self):
        try:
            self.srv.settimeout(0.5)
        except OSError:
            return
        while self._running:
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()

    def _handle_client(self, conn):
        try:
            self._handshake(conn)
            authorized = False
            while self._running:
                opcode, payload = _recv_frame(conn)
                if opcode == 0x8:  # close
                    return
                if opcode != 0x1:
                    continue
                req = json.loads(payload.decode())
                req_id = req.get("req_id")
                authorized = self._handle_request(conn, req, req_id, authorized)
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _handshake(self, conn):
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += conn.recv(4096)
        headers = buf.decode(errors="replace")
        key = None
        for line in headers.split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        accept = _accept_key(key)
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        )
        conn.sendall(response.encode())

    def _handle_request(self, conn, req, req_id, authorized):
        def reply(msg_type, data, extra=None):
            out = {"msg_type": msg_type, "req_id": req_id, msg_type: data}
            if extra:
                out.update(extra)
            _send_text(conn, out)

        def error(code, message):
            _send_text(conn, {"error": {"code": code, "message": message}, "req_id": req_id})

        if "authorize" in req:
            if req["authorize"] == VALID_TOKEN:
                reply("authorize", {"loginid": "CR9999999", "currency": "USD",
                                     "balance": 10000.0, "landing_company_name": "svg",
                                     "scopes": ["read", "trade"]})
                return True
            error("InvalidToken", "The token is invalid.")
            return False

        if not authorized:
            error("AuthorizationRequired", "Please authorize first.")
            return authorized

        if "balance" in req:
            reply("balance", {"balance": 10000.0, "currency": "USD", "id": "bal1"})
        elif "active_symbols" in req:
            reply("active_symbols", [
                {"symbol": "R_100", "display_name": "Volatility 100 Index", "market": "synthetic_index"},
                {"symbol": "frxEURUSD", "display_name": "EUR/USD", "market": "forex"},
                {"symbol": "cryBTCUSD", "display_name": "BTC/USD", "market": "cryptocurrency"},
            ])
        elif "ticks" in req:
            symbol = req["ticks"]
            reply("tick", {"symbol": symbol, "quote": 1234.56, "epoch": int(time.time())})
        elif "proposal" in req:
            reply("proposal", {"id": f"prop-{req_id}", "ask_price": req.get("amount", 10),
                                "payout": req.get("amount", 10) * 1.9, "spot": 1234.56})
        elif "buy" in req:
            cid = next(self._contract_ids)
            reply("buy", {"contract_id": cid, "buy_price": req.get("price", 10),
                           "longcode": "Mock contract", "shortcode": "MOCK", "transaction_id": cid})
        elif "portfolio" in req:
            reply("portfolio", {"contracts": []})
        elif "sell" in req:
            reply("sell", {"sold_for": req.get("price", 0), "contract_id": req.get("sell")})
        elif "profit_table" in req:
            reply("profit_table", {"transactions": [], "count": 0})
        elif "forget" in req or "forget_all" in req:
            _send_text(conn, {"msg_type": "forget", "req_id": req_id, "forget": 1})
        else:
            error("UnknownCall", f"Mock server does not implement {list(req.keys())}")
        return authorized
