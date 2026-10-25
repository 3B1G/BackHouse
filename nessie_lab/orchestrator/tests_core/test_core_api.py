"""Part C: the HTTP API for the frontend (/api/*) on the real core and a tmp root, called in-process through the
agent's GuardedServer ASGI app (no socket, no network, no ASI). Every endpoint: 401 without the token, 403 for a
disallowed origin, a correct CORS preflight for an allowed one (never "*"), JSON matching its model, and nothing ever
approves or applies (tripwire on CH and the owner key)."""
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from orchestrator import api
from orchestrator import agent as agent_mod

ALLOWED = "http://localhost:3000"
ORIGINS = f"{ALLOWED},http://127.0.0.1:5173"
BASE = "http://127.0.0.1:8941"
SCENARIO = {"name": "pizza up", "price_changes_cents": {"Margherita Pizza": 1900}}
ENDPOINTS = [("GET", "/api/health", None), ("GET", "/api/brief", None), ("POST", "/api/ask", {"question": "brief"}),
             ("GET", "/api/status", None), ("GET", "/api/suggestions", None),
             ("POST", "/api/suggestions/preview", {"id": "orch-none"}), ("GET", "/api/market", None),
             ("POST", "/api/forecast", {"scenario": SCENARIO}), ("GET", "/api/menu", None), ("GET", "/api/bills", None)]
MODELS = {path: resp for _, path, _, resp, _ in api.ENDPOINTS}


class InProcess(httpx.MockTransport):
    """The agent's ASGI app called in-process; a MockTransport so nothing reaches a socket."""

    def __init__(self, app, host):
        super().__init__(lambda request: None)
        self.inner = httpx.ASGITransport(app=app, client=(host, 50000))

    async def handle_async_request(self, request):
        return await self.inner.handle_async_request(request)


@pytest.fixture
async def served(root, monkeypatch, no_llm_key):
    import uagents.asgi

    monkeypatch.setattr(uagents.asgi, "HOST", "0.0.0.0")
    for name in ("SCOUT_ADDR", "BOOKKEEPER_ADDR", "ORCHESTRATOR_USE_AGENTS", "ORCHESTRATOR_PEERS",
                 "ORCHESTRATOR_MAILBOX", "ORCHESTRATOR_BIND_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ORCHESTRATOR_SEED", f"api test seed {uuid4().hex}")
    monkeypatch.setenv("ORCHESTRATOR_ALLOWED_ORIGINS", ORIGINS)
    a = agent_mod.make_agent(root, port=8941, llm_factory=lambda: SimpleNamespace(available=False))
    return a


def client(a, host="127.0.0.1"):
    return httpx.AsyncClient(transport=InProcess(a._server, host), base_url=BASE, timeout=120)


def auth(a, **extra):
    return {"Authorization": f"Bearer {a._server.token}", **extra}


async def call(c, method, path, body, headers):
    return await (c.get(path, headers=headers) if method == "GET" else c.post(path, json=body, headers=headers))


# ---------------------------------------------------------------- the guard, for every endpoint
@pytest.mark.parametrize("method,path,body", ENDPOINTS)
async def test_every_endpoint_needs_the_token_and_an_allowed_origin(served, method, path, body):
    async with client(served) as c:
        assert (await call(c, method, path, body, {})).status_code == 401
        assert (await call(c, method, path, body, {"Authorization": "Bearer wrong"})).status_code == 401
        r = await call(c, method, path, body, auth(served, Origin="https://evil.example"))
        assert r.status_code == 403 and "access-control-allow-origin" not in r.headers
        r = await call(c, method, path, body, auth(served, Origin="http://localhost:3001"))   # loopback, not listed
        assert r.status_code == 403
        r = await c.options(path, headers={"Origin": "https://evil.example", "Access-Control-Request-Method": method})
        assert r.status_code == 403 and "access-control-allow-origin" not in r.headers
        r = await c.options(path, headers={"Origin": ALLOWED, "Access-Control-Request-Method": method})
        assert r.status_code == 204
        assert r.headers["access-control-allow-origin"] == ALLOWED and r.headers["vary"] == "Origin"
        assert r.headers["access-control-allow-headers"] == "Authorization, Content-Type"
        assert r.headers["access-control-allow-methods"] == "GET, POST, OPTIONS"
        assert "*" not in "".join(v for k, v in r.headers.items() if k.startswith("access-control"))
        r = await call(c, method, path, body, {"Origin": ALLOWED})          # CORS is no substitute for the token
        assert r.status_code == 401 and r.headers["access-control-allow-origin"] == ALLOWED


async def test_allowed_origin_gets_the_echo_and_no_wildcard(served):
    async with client(served) as c:
        r = await c.get("/api/health", headers=auth(served, Origin="http://127.0.0.1:5173"))
        assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
        r = await c.get("/api/health", headers=auth(served))                 # curl: no Origin, no CORS headers
        assert r.status_code == 200 and not any(k.startswith("access-control") for k in r.headers)
        r = await c.options("/api/health", headers={"Origin": ALLOWED, "Access-Control-Request-Method": "DELETE"})
        assert r.status_code == 403


async def test_non_loopback_client_or_host_and_big_bodies_are_refused(served):
    async with client(served, host="10.1.2.3") as c:
        assert (await c.get("/api/health", headers=auth(served, **{"X-Forwarded-For": "127.0.0.1"}))).status_code == 403
    async with client(served) as c:
        assert (await c.get("/api/health", headers=auth(served, Host="evil.example"))).status_code == 403
        r = await c.post("/api/ask", content=b'{"question": "' + b"x" * 70_000 + b'"}',
                         headers=auth(served, **{"Content-Type": "application/json"}))
        assert r.status_code == 413


def test_origins_are_validated():
    assert agent_mod.parse_allowed_origins(" http://LocalHost:3000/ ,http://[::1]:8080") == {
        "http://localhost:3000", "http://[::1]:8080"}
    for bad in ["*", "http://*.example", "https://app.example.com", "http://localhost:3000/app", "ftp://localhost"]:
        with pytest.raises(ValueError):
            agent_mod.parse_allowed_origins(bad)


async def test_api_is_not_registered_off_loopback(root, monkeypatch):
    import uagents.asgi

    monkeypatch.setattr(uagents.asgi, "HOST", "0.0.0.0")
    monkeypatch.setenv("ORCHESTRATOR_SEED", f"api test seed {uuid4().hex}")
    monkeypatch.setenv("ORCHESTRATOR_BIND_HOST", "0.0.0.0")
    a = agent_mod.make_agent(root, port=8942)
    assert not [k for k in a._rest_handlers if k[1].startswith("/api")]


# ---------------------------------------------------------------- shapes, and nothing approves
async def test_every_endpoint_returns_its_model_and_never_approves(served, root, tripwire, open_gates):
    async with client(served) as c:
        h = auth(served, Origin=ALLOWED)
        got = {}
        for method, path, body in ENDPOINTS:
            r = await call(c, method, path, body, h)
            assert r.status_code == 200, (path, r.text)
            data = r.json()
            MODELS[path].parse_obj(data)                                       # matches the documented model
            assert set(data) == set(MODELS[path].__fields__), path
            got[path] = data
        # health
        hl = got["/api/health"]
        assert hl["mode"] == "local" and hl["offline"] is True and hl["delegation"] is False
        assert hl["agents"]["market_scout"]["configured"] is False and hl["bookkeeper"]["available"] is False
        # brief: read-only, money as cents + usd, ISO dates
        b = got["/api/brief"]
        assert b["ok"] and b["proposes"] is False and "Cash on " in b["text"]
        assert b["cash"]["total"]["usd"].startswith("$") and isinstance(b["cash"]["total"]["cents"], int)
        assert b["runway"]["run_out_likely"] == "2027-06-25" and b["bills"]["count"] >= 1
        assert b["market"]["available"] and b["findings"]["available"] is False
        # ask "brief" proposed (owner): suggestion paths with approve commands
        ask = got["/api/ask"]
        assert ask["ok"] and ask["suggestions"] and ask["owner_action_required"] is True
        assert all("changes" in cmd and " approve " in cmd for cmd in ask["approve_commands"])
        assert ask["text"].endswith("Nothing has been changed; approve at your terminal to apply.")
        # the rest
        assert got["/api/status"]["cost_price"] is not None and "available" in got["/api/status"]["bookkeeper_status"]
        assert got["/api/suggestions/preview"]["ok"] is False                 # unknown id: refused, not an error 500
        f = got["/api/forecast"]
        assert f["ok"] and f["base"]["run_out_likely"] and f["vs_base"]["base_forecast_id"] == f["base"]["forecast_id"]
        m = got["/api/menu"]
        assert m["ok"] and m["dishes"] and 0 < m["food_cost_pct"] < 1 and m["food_cost_display"].endswith("%")
        assert got["/api/bills"]["bills"]["count"] == 3 and got["/api/market"]["market"]["available"]

        r = (await c.get("/api/suggestions", headers=h)).json()
        assert r["ok"] and len(r["suggestions"]) == len(ask["suggestions"])
        for s in r["suggestions"]:
            assert s["owner_action_required"] is True and s["approve_command"] and s["status"] == "pending"
            assert s["plan_line"] and s["created"] and s["path"].startswith("data/suggestions/")
        rep = next(s for s in r["suggestions"] if s["action"] == "reprice_item")
        assert rep["old_price"]["cents"] == 1750 and rep["new_price"]["usd"].startswith("$")
        r = (await c.post("/api/suggestions/preview", json={"id": rep["id"]}, headers=h)).json()
        assert r["ok"] and r["question"] == rep["plan_line"] and r["owner_action_required"] is True
        assert r["dry_run"]["applied"] is False and r["approve_command"] == rep["approve_command"]
        st = (await c.get("/api/status", headers=h)).json()
        assert st["pending_suggestions"] == len(ask["suggestions"])
    assert tripwire == []


async def test_brief_get_writes_no_suggestion(served, root, open_gates):
    async with client(served) as c:
        r = await c.get("/api/brief", headers=auth(served))
    folder = root / "data" / "suggestions"
    assert r.json()["ok"] and r.json()["fixes"] == []
    assert not folder.exists() or not list(folder.glob("*.json"))
    assert "writes no suggestion" in r.json()["text"]


async def test_forecast_validates_the_scenario(served):
    async with client(served) as c:
        h = auth(served)
        for scenario in [{}, {"name": "nothing"}, {"teleport": 1}, {"price_changes_cents": {"Pineapple": 100}}]:
            r = await c.post("/api/forecast", json={"scenario": scenario}, headers=h)
            assert r.status_code == 200 and r.json()["ok"] is False and r.json()["error"]
        assert (await c.post("/api/forecast", json={"scenario": "x"}, headers=h)).status_code == 400
        assert (await c.post("/api/forecast", json={}, headers=h)).status_code == 400


async def test_preview_refuses_paths(served):
    async with client(served) as c:
        for bad in ["../../books", "..\\x", "a/b", "", "x" * 80]:
            r = (await c.post("/api/suggestions/preview", json={"id": bad}, headers=auth(served))).json()
            assert r["ok"] is False


async def test_no_route_can_approve_or_apply(served):
    paths = [p for _, p in served._rest_handlers]
    assert not [p for p in paths if any(w in p.lower() for w in ("approve", "apply", "revert", "unlock", "live"))]
    api_paths = sorted(p for p in paths if p.startswith("/api"))
    assert api_paths == sorted(p for _, p, *_ in api.ENDPOINTS)


def test_api_source_never_approves_or_applies():
    import re
    from pathlib import Path
    src = (Path(api.__file__)).read_text(encoding="utf-8")
    assert not re.findall(r"live\s*=\s*True|record_approval|\bunlock\(|read_passphrase|ask_owner|read_key|"
                          r"\.revert\(|\.apply\(|Executor\(", src)


async def test_health_reports_reachability_with_a_probe(root, monkeypatch, no_llm_key):
    import uagents.asgi

    monkeypatch.setattr(uagents.asgi, "HOST", "0.0.0.0")
    monkeypatch.setenv("ORCHESTRATOR_SEED", f"api test seed {uuid4().hex}")
    monkeypatch.setenv("SCOUT_ADDR", "agent1qscout")
    monkeypatch.setenv("BOOKKEEPER_ADDR", "agent1qbookkeeper")
    monkeypatch.setenv("ORCHESTRATOR_PEERS", "agent1qscout=http://127.0.0.1:8006/submit")
    a = agent_mod.make_agent(root, port=8943)
    probed = []

    async def probe(endpoint):
        probed.append(endpoint)
        return True

    a.orchestrator.probe = probe
    async with client(a) as c:
        hl = (await c.get("/api/health", headers=auth(a))).json()
    assert hl["delegation"] is True and probed == ["http://127.0.0.1:8006/submit"]
    assert hl["agents"]["market_scout"]["reachable"] is True
    assert hl["agents"]["bookkeeper"]["reachable"] is None and "ORCHESTRATOR_PEERS" in hl["agents"]["bookkeeper"]["how"]


# ---------------------------------------------------------------- api-schema
def test_api_schema_cli_writes_valid_json_and_the_committed_copy_is_current(tmp_path, capsys):
    from pathlib import Path

    from orchestrator import cli

    out = tmp_path / "schema.json"
    assert cli.main(["api-schema", "--out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert [e["path"] for e in data["endpoints"]] == [p for _, p, *_ in api.ENDPOINTS]
    assert set(data["models"]) >= {"BriefResponse", "AskApiRequest", "ForecastRequest", "SuggestionsResponse"}
    assert "approve_command" in json.dumps(data["models"]["SuggestionsResponse"])
    committed = Path(api.__file__).resolve().parents[2] / "docs" / "api_schema.json"
    assert json.loads(committed.read_text(encoding="utf-8")) == data, "rerun `orchestrator api-schema --out docs/api_schema.json`"


async def test_bookkeeper_route_refuses_web_origins(served):
    async with client(served) as c:
        body = {"op": "update"}
        assert (await c.post("/bookkeeper", json=body)).status_code == 401
        r = await c.post("/bookkeeper", json=body, headers=auth(served, Origin=ALLOWED))
        assert r.status_code == 403                                           # even an allowed frontend origin
