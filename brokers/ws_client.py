"""
A minimal RFC 6455 WebSocket client built on nothing but the stdlib.

Why this exists: `websocket-client` / `websockets` are not installed in this
environment and there is no network access to `pip install` them. Deriv's
API is WebSocket-only, so a real integration needs a real WS client one way
or another. This implements just enough of RFC 6455 to talk to it:
  - the HTTP Upgrade handshake (incl. Sec-WebSocket-Accept verification)
  - text-frame send/receive with client-to-server masking
  - ping/pong and close-frame handling
  - TLS via the stdlib `ssl` module for wss://

It is intentionally scoped to what a JSON-over-WebSocket API like Deriv's
needs (single-frame text messages) rather than being a general-purpose
WebSocket implementation — no permessage-deflate, no fragmented-message
reassembly beyond simple continuation, no server mode.
"""
import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
from urllib.parse import urlparse

OPCODE_CONT = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketError(Exception):
    pass


class WebSocketClient:
    """Blocking, thread-safe-enough-for-one-reader-one-writer WS client."""

    def __init__(self, url, connect_timeout=10, read_timeout=10):
        self.url = url
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.sock = None
        self._send_lock = threading.Lock()
        self._closed = True

    # ---------------------------------------------------------------- open
    def connect(self):
        parsed = urlparse(self.url)
        if parsed.scheme not in ("ws", "wss"):
            raise WebSocketError(f"Unsupported scheme: {parsed.scheme}")
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        raw = socket.create_connection((host, port), timeout=self.connect_timeout)
        if parsed.scheme == "wss":
            ctx = ssl.create_default_context()
            self.sock = ctx.wrap_socket(raw, server_hostname=host)
        else:
            self.sock = raw
        self.sock.settimeout(self.read_timeout)

        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        self.sock.sendall(request.encode())

        response = self._read_http_response()
        status_line = response.split("\r\n", 1)[0]
        if " 101 " not in status_line:
            raise WebSocketError(f"Handshake failed: {status_line} | {response.split(chr(13)+chr(10)+chr(13)+chr(10))[0]}")

        expected_accept = base64.b64encode(
            hashlib.sha1((key + GUID).encode()).digest()
        ).decode()
        if f"Sec-WebSocket-Accept: {expected_accept}" not in response and \
           expected_accept not in response:
            raise WebSocketError("Handshake failed: Sec-WebSocket-Accept mismatch")

        self._closed = False

    def _read_http_response(self):
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("Connection closed during handshake")
            buf += chunk
        return buf.decode(errors="replace")

    # --------------------------------------------------------------- send
    def send_text(self, text):
        self._send_frame(OPCODE_TEXT, text.encode("utf-8"))

    def _send_frame(self, opcode, payload):
        if self._closed:
            raise WebSocketError("Socket is closed")
        fin_opcode = 0x80 | opcode
        length = len(payload)
        mask_key = os.urandom(4)

        if length <= 125:
            header = struct.pack("!BB", fin_opcode, 0x80 | length)
        elif length <= 0xFFFF:
            header = struct.pack("!BBH", fin_opcode, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", fin_opcode, 0x80 | 127, length)

        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            self.sock.sendall(header + mask_key + masked)

    # --------------------------------------------------------------- recv
    def recv(self):
        """Returns the next text message as a str, or None on ping/pong
        (handled internally) / raises WebSocketError on close/error."""
        opcode, payload = self._read_frame()
        if opcode == OPCODE_PING:
            self._send_frame(OPCODE_PONG, payload)
            return None
        if opcode == OPCODE_PONG:
            return None
        if opcode == OPCODE_CLOSE:
            self._closed = True
            raise WebSocketError("Server closed the connection")
        if opcode in (OPCODE_TEXT, OPCODE_CONT):
            return payload.decode("utf-8", errors="replace")
        return None

    def _read_frame(self):
        header = self._recv_exact(2)
        b0, b1 = header[0], header[1]
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F

        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]

        mask_key = self._recv_exact(4) if masked else None
        payload = self._recv_exact(length) if length else b""
        if masked:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            try:
                chunk = self.sock.recv(n - len(buf))
            except OSError as e:
                raise WebSocketError(f"Socket read failed: {e}")
            if not chunk:
                raise WebSocketError("Connection closed unexpectedly")
            buf += chunk
        return buf

    # -------------------------------------------------------------- close
    def close(self):
        if self._closed:
            return
        try:
            self._send_frame(OPCODE_CLOSE, b"")
        except Exception:
            pass
        self._closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
