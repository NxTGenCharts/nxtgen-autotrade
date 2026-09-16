"""
Lightweight pub/sub for real-time updates.

Phase 1/2 note: the spec's target architecture uses Socket.IO/WebSockets
backed by Redis so multiple API processes can fan out events. This
single-process build doesn't have network access to install
flask-socketio or redis, so this is an in-memory equivalent: one queue per
connected browser tab, keyed by user_id, fed by Server-Sent Events (SSE) —
a real push channel (no polling) that works over plain HTTP and needs zero
extra dependencies. Swapping this for Socket.IO + Redis later is a
drop-in replacement: every call site below (`publish(user_id, channel,
payload)`) stays identical; only this file's internals change.

Channels mirror the spec's WebSocket channel list: account:update,
bot:update, bot:trade, bot:position, bot:log, risk:event, broker:status,
notification:new.
"""
import queue
import threading
import time

_lock = threading.Lock()
_subscribers = {}  # user_id -> set of Queue


def subscribe(user_id):
    q = queue.Queue(maxsize=200)
    with _lock:
        _subscribers.setdefault(user_id, set()).add(q)
    return q


def unsubscribe(user_id, q):
    with _lock:
        subs = _subscribers.get(user_id)
        if subs and q in subs:
            subs.discard(q)
            if not subs:
                _subscribers.pop(user_id, None)


def publish(user_id, channel, payload):
    with _lock:
        subs = list(_subscribers.get(user_id, ()))
    event = {"channel": channel, "payload": payload, "t": time.time()}
    for q in subs:
        try:
            q.put_nowait(event)
        except queue.Full:
            pass  # slow/dead client — drop rather than block the publisher
