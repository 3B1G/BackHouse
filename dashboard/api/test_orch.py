"""The /api/orch proxy and the app's catch-all, against a fake orchestrator (no network). From the repo root:

    uv run --project dashboard pytest dashboard/api
"""
import sys
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
import main  # noqa: E402
import orch  # noqa: E402


class FakeOrchestrator:
    """Stands in for requests.request: remembers each call and answers like the orchestrator (200, ok: false on errors)."""

    def __init__(self, status=200, data=None, error=None):
        self.calls, self.status, self.data, self.error = [], status, data or {"ok": True}, error

    def __call__(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json, "headers": dict(headers or {})})
        if self.error:
            raise self.error
        res = requests.Response()
        res.status_code = self.status
        res._content = __import__("json").dumps(self.data).encode()
        return res


@pytest.fixture
def token(tmp_path, monkeypatch):
    path = tmp_path / "rest_token"
    monkeypatch.setattr(orch, "TOKEN", path)
    return path


@pytest.fixture
def fake(monkeypatch):
    f = FakeOrchestrator()
    monkeypatch.setattr(orch.requests, "request", f)
    return f


client = TestClient(main.app)


def test_reads_the_token_on_every_call(token, fake):
    token.write_text("first\n")
    assert client.get("/api/orch/health").json() == {"ok": True}
    token.write_text("second\n")                        # `serve` restarted with a new token
    client.get("/api/orch/review")
    assert [c["headers"]["Authorization"] for c in fake.calls] == ["Bearer first", "Bearer second"]
    assert fake.calls[1]["url"].endswith("/api/review")


def test_token_never_reaches_the_browser(token, fake):
    token.write_text("secret-token")
    res = client.get("/api/orch/brief")
    assert "secret-token" not in res.text and "secret-token" not in str(res.headers)


def test_browser_origin_is_not_forwarded(token, fake):
    token.write_text("t")
    client.get("/api/orch/suggestions", headers={"Origin": "http://localhost:5173"})
    assert {k.lower() for k in fake.calls[0]["headers"]} == {"authorization", "accept"}


@pytest.mark.parametrize("origin", ["http://evil.example", "http://localhost:3000", "null", "https://localhost:5173"])
def test_other_origins_are_turned_away(token, fake, origin):
    token.write_text("t")
    assert client.get("/api/orch/review", headers={"Origin": origin}).status_code == 403
    assert client.post("/api/orch/review/decide", json={"accept_verified_errors": True},
                       headers={"Origin": origin}).status_code == 403
    assert fake.calls == []


def test_missing_token_is_503(token, fake):
    res = client.get("/api/orch/health")
    assert res.status_code == 503 and "orchestrator serve" in res.json()["detail"]
    assert fake.calls == []


def test_orchestrator_down_is_503(token, monkeypatch):
    token.write_text("t")
    monkeypatch.setattr(orch.requests, "request", FakeOrchestrator(error=requests.ConnectionError("refused")))
    assert client.get("/api/orch/review").status_code == 503


@pytest.mark.parametrize("status", [401, 403])
def test_guard_refusal_is_502(token, monkeypatch, status):
    token.write_text("t")
    monkeypatch.setattr(orch.requests, "request", FakeOrchestrator(status=status, data={"detail": "no"}))
    assert client.get("/api/orch/review").status_code == 502


def test_ok_false_passes_through(token, monkeypatch):
    token.write_text("t")
    body = {"ok": False, "error": "the review is locked", "proposed": [], "committed": False}
    monkeypatch.setattr(orch.requests, "request", FakeOrchestrator(data=body))
    res = client.post("/api/orch/review/decide", json={"finding": "F-1", "decision": "accept"})
    assert res.status_code == 200 and res.json() == body


def test_decide_forwards_only_what_was_given(token, fake):
    token.write_text("t")
    client.post("/api/orch/review/decide", json={"finding": "F-1", "decision": "revise", "note": "split it"})
    client.post("/api/orch/review/decide", json={"accept_verified_errors": True})
    client.post("/api/orch/review/decide", json={"finding": "F-2", "decision": "dismiss", "accept_verified_errors": False})
    assert [c["json"] for c in fake.calls] == [{"finding": "F-1", "decision": "revise", "note": "split it"},
                                               {"accept_verified_errors": True},
                                               {"finding": "F-2", "decision": "dismiss"}]


@pytest.mark.parametrize("body", [{"finding": "F-1", "decision": "approve"}, {"finding": "x" * 81, "decision": "accept"},
                                  {"finding": "F-1", "decision": "revise", "note": "n" * 501}])
def test_decide_rejects_bad_bodies(token, fake, body):
    token.write_text("t")
    assert client.post("/api/orch/review/decide", json=body).status_code == 422
    assert fake.calls == []


def test_app_files_stay_inside_dist(tmp_path):
    web = tmp_path / "dist"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("app")
    (web / "assets" / "a.js").write_text("js")
    (tmp_path / "secret.txt").write_text("no")
    index = web.resolve() / "index.html"
    assert main.app_file(web, "assets/a.js") == (web / "assets" / "a.js").resolve()
    assert main.app_file(web, "") == index
    assert main.app_file(web, "../secret.txt") == index
    assert main.app_file(web, "assets/../../secret.txt") == index
    assert main.app_file(web, str((tmp_path / "secret.txt").resolve())) == index

    def touched(self, *a, **k):
        raise AssertionError(f"touched the filesystem for {self}")

    root = web.resolve()
    with pytest.MonkeyPatch.context() as mp:               # on Windows, resolving //host/share connects to that host
        mp.setattr(main.Path, "resolve", lambda self, *a, **k: root if self == web else touched(self))
        mp.setattr(main.Path, "is_file", touched)
        for bad in ("//evil.example/share/x", "\\\\evil.example\\share\\x", "/etc/passwd", "C:secret.txt",
                    "assets\\..\\..\\secret.txt", "assets/a.js\0", "a/../../x"):
            assert main.app_file(web, bad) == index, bad
