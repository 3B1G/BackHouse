"""Fixtures for the ASI-facing tests (test_llm, test_guard, test_turn, test_agent use fake_core via pytestmark): the fake core installed, no ASI key from the environment or .env, and no real network."""
import sys
import types

import httpx
import pytest

import fakes
from orchestrator import llm


@pytest.fixture
def fake_core(monkeypatch):
    fakes.install(monkeypatch)
    for name in ("ORCHESTRATOR_API_KEY", "ASI1_API_KEY", "AGENTVERSE_TOKEN", "ORCHESTRATOR_PEERS",
                 "ORCHESTRATOR_ALLOWED_SENDERS", "ORCHESTRATOR_CARDS", "ORCHESTRATOR_USE_AGENTS", "SCOUT_ADDR",
                 "BOOKKEEPER_ADDR", "ORCHESTRATOR_MAILBOX", "ORCHESTRATOR_MODEL", "ORCHESTRATOR_BIND_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(llm, "load_env", lambda: None)          # never read the real key from .env

    real_send = httpx.AsyncClient.send

    async def guarded_send(self, request, *a, **kw):
        if not isinstance(self._transport, httpx.MockTransport):
            raise AssertionError(f"real network call attempted to {request.url.host}")
        return await real_send(self, request, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "send", guarded_send)


@pytest.fixture
def tripwire(monkeypatch):
    """Fail the test if anything approves, applies or unlocks the owner key."""
    hits = []

    def boom(name):
        def fail(*a, **kw):
            hits.append(name)
            raise AssertionError(f"{name} must never be called by the orchestrator")
        return fail

    try:
        import changes.executor as executor
    except ImportError:
        executor = types.ModuleType("changes.executor")
        executor.Executor = type("Executor", (), {"apply": None, "record_approval": None})
        pkg = types.ModuleType("changes")
        pkg.executor = executor
        monkeypatch.setitem(sys.modules, "changes", pkg)
        monkeypatch.setitem(sys.modules, "changes.executor", executor)
    monkeypatch.setattr(executor.Executor, "apply", boom("Executor.apply"))
    monkeypatch.setattr(executor.Executor, "record_approval", boom("Executor.record_approval"))
    try:
        import cost_price.api as api
        monkeypatch.setattr(api, "record_approval", boom("api.record_approval"), raising=False)
    except ImportError:
        pass
    try:
        import cost_price.owner as owner
        monkeypatch.setattr(owner, "unlock", boom("owner.unlock"), raising=False)
    except ImportError:
        pass
    return hits
