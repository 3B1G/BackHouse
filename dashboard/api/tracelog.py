"""A live log of agent calls for the developer page: who was called, with what, how long it took, and what came back.

The check's steps, the Forecaster, the Chat's model calls and its tools all write here, and /api/dev/trace streams it
(Server-Sent Events): everything still in memory first, then each new event as it happens. Memory only, the last 800 events.
"""
import itertools
import json
import threading
import time
import uuid
from collections import deque

_EVENTS = deque(maxlen=800)
_IDS = itertools.count(1)
_COND = threading.Condition()
_BOOT = uuid.uuid4().hex[:6]      # event ids restart with the API, so reconnects say which run of the API they saw


def emit(agent, kind, text, group=None, ms=None, **detail):
    """kind: start, done, waiting (a check's step), call (an agent was asked something), log (a line it printed),
    result (what came back) or error. group ties events together: a check's run id, chat-<id> for one Chat question."""
    detail = {k: v for k, v in detail.items() if v is not None}
    with _COND:
        _EVENTS.append({"id": next(_IDS), "at": round(time.time(), 3), "agent": agent, "kind": kind, "text": str(text)[:400],
                        "group": group, "ms": ms, "detail": detail or None})
        _COND.notify_all()


class Span:
    """One call: logged when it starts, then its result (or error) with how long it took."""

    def __init__(self, agent, text, group=None, **detail):
        self.agent, self.group, self.t0 = agent, group, time.perf_counter()
        emit(agent, "call", text, group, **detail)

    def _ms(self):
        return round((time.perf_counter() - self.t0) * 1000)

    def done(self, text, **detail):
        emit(self.agent, "result", text, self.group, self._ms(), **detail)

    def fail(self, text, **detail):
        emit(self.agent, "error", text, self.group, self._ms(), **detail)


def stream(last_event_id=None):
    """SSE: what is in memory, then each new event. A reconnecting browser sends the last id it saw (Last-Event-ID) and
    picks up after it; ids from before an API restart start it over. Runs until the browser goes away."""
    yield ": connected\n\n"     # proxies (Vite's dev server) hold the headers until the first bytes, so send some now
    boot, _, seen = (last_event_id or "").partition(":")
    last = int(seen) if boot == _BOOT and seen.isdigit() else 0
    while True:
        with _COND:
            fresh = [e for e in _EVENTS if e["id"] > last]
            if not fresh:
                _COND.wait(timeout=15)
                fresh = [e for e in _EVENTS if e["id"] > last]
        for e in fresh:
            last = e["id"]
            yield f"id: {_BOOT}:{e['id']}\ndata: {json.dumps(e, default=str)}\n\n"
        if not fresh:
            yield ": idle\n\n"
