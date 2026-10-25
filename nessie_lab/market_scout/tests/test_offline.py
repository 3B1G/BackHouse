"""Offline mode (saved pages only: no HTTP client, no DNS, no Apify, no ASI) and MARKET_SCOUT_PEERS."""
import json
import socket
import sys
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from conftest import PAGES, FakePages, Resolver, store
from market_scout import agent as agent_mod
from market_scout import cli, competitors
from market_scout.agent import allowed_senders, local_client_address, make_agent, parse_peers, resolver_rules
from market_scout.contracts import MarketRequest
from market_scout.extract import ASIReader, rule_reader
from market_scout.pages import OfflineMiss, PageStore
from market_scout.runner import MarketScout, env_offline
from test_agent import handler_for, send

HTML = {"content-type": "text/html; charset=utf-8"}
RealClient = httpx.Client                                 # the wire fixture replaces httpx.Client


def online_handler(request):
    url = f"https://{request.headers['host']}{request.url.path}"
    if url not in PAGES:
        return httpx.Response(404)
    return httpx.Response(200, content=PAGES[url].encode(), headers=HTML)


class Tripwire:
    """Network that fails the test: every call is recorded (a scan catches per-page errors, so raising alone would
    only turn into a limitation) and raises."""

    def __init__(self):
        self.calls = []

    def hit(self, what):
        self.calls.append(what)
        raise AssertionError(f"network used offline: {what}")

    def resolver(self, host, port):
        self.hit(f"dns {host}")

    def client(self, *a, **kw):
        self.hit("httpx.Client()")

    def __getattr__(self, name):                          # an injected client object: any use is a hit
        if name.startswith("_") or name in ("calls", "hit", "resolver", "client"):
            raise AttributeError(name)
        self.hit(f"client.{name}")


@pytest.fixture
def wire(monkeypatch):
    """No way out: httpx.Client, DNS, sockets, Apify and ASI are all tripwires."""
    t = Tripwire()
    monkeypatch.setattr(httpx, "Client", t.client)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: t.hit("getaddrinfo"))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: t.hit("create_connection"))
    monkeypatch.setattr(competitors, "from_apify", lambda *a, **k: t.hit("apify"))
    monkeypatch.setenv("APIFY_TOKEN", "apify-token")
    monkeypatch.setenv("ASI1_API_KEY", "asi-key")
    return t


def save_pages(root, urls=None):
    """Fetch the fake pages once (online, MockTransport) into root/data/market_pages."""
    client = RealClient(transport=httpx.MockTransport(online_handler), follow_redirects=False)
    s = PageStore(root / "data" / "market_pages", client=client, resolver=Resolver())
    for url in urls or PAGES:
        s.get(url)
    return s.dir


def offline_scout(root, wire, **kw):
    pages = PageStore(root / "data" / "market_pages", client=wire, resolver=wire.resolver, offline=True)
    return MarketScout(root, pages=pages, use_apify=None, offline=True, **kw)


def test_every_code_path_offline_uses_no_network(root, wire):
    save_pages(root)
    meta = next((root / "data" / "market_pages").glob("*.json"))
    meta.unlink()                                          # one page missing too
    asi = ASIReader(api_key="k", transport=httpx.MockTransport(lambda r: wire.hit("asi transport")))
    for readers in (None, [rule_reader, asi]):             # default readers (ASI key set) and an injected ASI reader
        s = offline_scout(root, wire, readers=readers)
        report, _, _ = s.scan(MarketRequest(request_id="o-1", refresh=True), "M-o-1")
        assert any("ASI menu reader skipped" in x for x in report.limitations)
        assert any("Apify (Google Maps) discovery skipped; no cached list" in x for x in report.limitations)
    s.page_status()
    assert wire.calls == []


def test_saved_copy_is_used_even_when_stale_and_refresh_is_asked(tmp_path, wire):
    save_pages(tmp_path, ["https://a.example/menu"])
    meta = next((tmp_path / "data" / "market_pages").glob("*.json"))
    m = json.loads(meta.read_text(encoding="utf-8"))
    m["retrieved_at"] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    meta.write_text(json.dumps(m), encoding="utf-8")
    s = PageStore(tmp_path / "data" / "market_pages", client=wire, resolver=wire.resolver, offline=True)
    assert "Margherita" in s.get("https://a.example/menu", refresh=True).text
    online = PageStore(tmp_path / "data" / "market_pages", client=wire, resolver=wire.resolver)
    assert "Margherita" in online.get("https://a.example/menu", refresh=True, offline=True).text   # per call
    with pytest.raises(OfflineMiss, match="offline, no saved copy"):
        s.get("https://b.example/menu")
    with pytest.raises(OfflineMiss):
        s.get("https://b.example/menu", offline=False)    # a call can't turn the store's offline mode off
    assert wire.calls == []


def test_missing_copy_is_a_limitation_and_the_rest_of_the_scan_runs(root, wire):
    save_pages(root, ["https://a.example/menu", "https://c.example/menu"])          # Beta Pizzeria not saved
    report, _, _ = offline_scout(root, wire, readers=[rule_reader]).scan(MarketRequest(request_id="m"), "M-m")
    assert "Beta Pizzeria: menu not read (offline, no saved copy)" in report.limitations
    assert any(x.startswith("offline: nothing fetched; using saved copy from ") for x in report.limitations)
    assert len(report.positions) == 3
    assert {p.restaurant for p in report.prices} == {"Alpha Trattoria", "Gamma Osteria"}
    assert wire.calls == []


def test_offline_results_equal_online_results_with_the_same_snapshots(root, monkeypatch):
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    monkeypatch.delenv("ASI1_API_KEY", raising=False)
    online = MarketScout(root, readers=[rule_reader], pages=store(root / "data", online_handler), use_apify=False,
                         offline=False)
    online.pages.dir = root / "data" / "market_pages"
    a, _, _ = online.scan(MarketRequest(request_id="on"), "M-on")
    t = Tripwire()
    off = PageStore(root / "data" / "market_pages", client=t, resolver=t.resolver, offline=True)
    b, _, _ = MarketScout(root, readers=[rule_reader], pages=off, use_apify=False, offline=True).scan(
        MarketRequest(request_id="on"), "M-on")
    dump = lambda r, k: [x.model_dump(mode="json") for x in getattr(r, k)]
    for key in ("positions", "summaries", "prices", "unreviewed", "rejected", "competitors"):
        assert dump(a, key) == dump(b, key), key
    assert [x for x in b.limitations if not x.startswith("offline:")] == a.limitations and t.calls == []


def test_env_var_and_flag_both_switch_offline(root, monkeypatch, capsys, wire):
    monkeypatch.delenv("MARKET_SCOUT_OFFLINE", raising=False)
    assert env_offline() is False and MarketScout(root).offline is False
    for value in ("1", "true", "YES"):
        monkeypatch.setenv("MARKET_SCOUT_OFFLINE", value)
        assert env_offline() is True and MarketScout(root).pages.offline is True
    monkeypatch.setenv("MARKET_SCOUT_OFFLINE", "maybe")
    with pytest.raises(ValueError, match="MARKET_SCOUT_OFFLINE"):
        env_offline()
    # the CLI reads root/data/market_pages, where nothing is saved here
    for i, (argv, env) in enumerate([(["--offline"], None), ([], "1")]):
        if env is None:
            monkeypatch.delenv("MARKET_SCOUT_OFFLINE", raising=False)
        else:
            monkeypatch.setenv("MARKET_SCOUT_OFFLINE", env)
        monkeypatch.setattr(sys, "argv", ["market-scout", "--root", str(root), "run", "--request-id", f"cli-{i}",
                                          *argv])
        cli.main()
        out = capsys.readouterr().out
        assert "Alpha Trattoria: menu not read (offline, no saved copy)" in out
        assert "offline: nothing fetched, and no saved copy of any menu page" in out
    assert wire.calls == []


async def test_serve_flag_and_env_reach_the_agent(root, monkeypatch):
    seen = []
    monkeypatch.setattr(agent_mod, "make_agent", lambda root, port, local_only, offline: seen.append(offline) or
                        type("A", (), {"run": lambda self: None})())
    for argv, env in ((["--offline"], ""), ([], "1"), ([], "0")):
        monkeypatch.setenv("MARKET_SCOUT_OFFLINE", env)
        monkeypatch.setattr(sys, "argv", ["market-scout", "--root", str(root), "serve", "--local-only", *argv])
        cli.main()
    assert seen == [True, True, False]
    made = []
    monkeypatch.setattr(agent_mod, "MarketScout", lambda root, offline: made.append(offline) or MarketScout(
        root, offline=offline, use_apify=False))
    monkeypatch.setenv("MARKET_SCOUT_SEED", "scout-test-seed-" + "x" * 32)
    monkeypatch.setenv("MARKET_SCOUT_OFFLINE", "1")
    monkeypatch.delenv("MARKET_SCOUT_PEERS", raising=False)
    make_agent(root, local_only=True)                      # offline=None: the scout reads MARKET_SCOUT_OFFLINE
    assert made == [None]


async def test_a_chat_request_cannot_turn_operator_offline_mode_off(root, monkeypatch):
    monkeypatch.delenv("ASI1_API_KEY", raising=False)
    pages = FakePages()
    service = MarketScout(root, readers=[rule_reader], pages=pages, use_apify=False, offline=True)
    body, _ = await send(root, json.dumps({"request_id": "c-1", "offline": False}), service, handler_for(service))
    assert body["status"] == "ok" and pages.calls and all(pages.offline_calls)
    report = json.loads((root / body["filename"]).read_text(encoding="utf-8"))
    assert report["limitations"][0].startswith("offline: nothing fetched")
    pages = FakePages()                                    # an online scout: a request may ask for offline
    service = MarketScout(root, readers=[rule_reader], pages=pages, use_apify=False, offline=False)
    body, _ = await send(root, json.dumps({"request_id": "c-2", "offline": True}), service, handler_for(service))
    assert body["status"] == "ok" and all(pages.offline_calls)
    pages = FakePages()
    service = MarketScout(root, readers=[rule_reader], pages=pages, use_apify=False, offline=False)
    await send(root, json.dumps({"request_id": "c-3"}), service, handler_for(service))
    assert not any(pages.offline_calls)


def test_offline_apify_uses_the_cache_whatever_its_age_or_none(tmp_path, wire):
    here = dict(lat=37.8, lng=-122.4)
    cache = tmp_path / "apify_cache.json"
    notes = []
    assert competitors.apify_rows(here, "t", cache, refresh=True, offline=True, notes=notes) == []
    assert "no cached list" in notes[0]
    row = dict(name="Epsilon Pizza", cuisine="Pizza", price_level="$$", distance_miles=0.3,
               menu_url="https://e.example/menu", found_via="apify_google_maps", closed=False)
    cache.write_text(json.dumps({"at": "2020-01-01T00:00:00+00:00", "here": [37.8, -122.4], "rows": [row]}),
                     encoding="utf-8")
    notes = []
    found = competitors.find_competitors(here, [], None, cache, refresh=True, offline=True, notes=notes)
    assert [c.name for c, _ in found] == ["Epsilon Pizza"] and "cached list from 2020-01-01" in notes[0]
    assert wire.calls == []


def test_pages_command_lists_saved_copies_with_dates(root, monkeypatch, capsys, wire):
    save_pages(root, ["https://a.example/menu", "https://b.example/menu"])
    status = MarketScout(root, offline=True, use_apify=False).page_status()
    by_url = {url: meta for _, url, meta in status}
    assert set(by_url) == {"https://a.example/menu", "https://b.example/menu", "https://c.example/menu"}   # Delta: dropped
    assert by_url["https://c.example/menu"] is None and by_url["https://a.example/menu"]["kind"] == "html"
    monkeypatch.setattr(sys, "argv", ["market-scout", "--root", str(root), "pages"])
    with pytest.raises(SystemExit) as exit_:
        cli.main()
    out = capsys.readouterr().out
    assert exit_.value.code == 1 and "2 of 3 menu pages have a saved copy; 1 missing" in out
    assert "MISSING" in out and "saved 20" in out and wire.calls == []
    save_pages_all = PageStore(root / "data" / "market_pages", client=wire, resolver=wire.resolver)
    assert save_pages_all.saved("https://d.example/menu") is None


# ---------------------------------------------------------------- MARKET_SCOUT_PEERS
PEER = "agent1qorchestrator0ravneet"


async def test_peers_are_merged_into_the_local_resolver(root, monkeypatch):
    monkeypatch.setenv("MARKET_SCOUT_PEERS", f" {PEER}=http://127.0.0.1:8005/submit , ")
    monkeypatch.setenv("MARKET_SCOUT_ALLOWED_SENDERS", PEER)
    monkeypatch.setenv("MARKET_SCOUT_SEED", "scout-test-seed-" + "x" * 32)
    monkeypatch.delenv("MARKET_SCOUT_MAILBOX", raising=False)
    rules = resolver_rules(root)
    assert rules[PEER] == "http://127.0.0.1:8005/submit" and local_client_address(root) in rules
    assert make_agent(root)._resolver._rules == rules


async def test_a_peer_not_on_the_allowlist_is_still_refused(root, monkeypatch):
    monkeypatch.setenv("MARKET_SCOUT_PEERS", f"{PEER}=http://localhost:8005/submit")
    monkeypatch.setenv("MARKET_SCOUT_ALLOWED_SENDERS", "agent1someoneelse")
    pages = FakePages()
    service = MarketScout(root, readers=[rule_reader], pages=pages, use_apify=False, offline=False)
    handler = handler_for(service, allowed=allowed_senders(root))
    body, _ = await send(root, json.dumps({"request_id": "p-1"}), service, handler, sender=PEER)
    assert body == {"status": "error", "error": "sender not allowed"} and pages.calls == []
    assert PEER not in allowed_senders(root)


@pytest.mark.parametrize("entry, error", [
    ("agent1abc", "expected agent1"),
    ("=http://127.0.0.1:8005/submit", "expected agent1"),
    ("bob=http://127.0.0.1:8005/submit", "starting with agent1"),
    ("agent1ABC=http://127.0.0.1:8005/submit", "starting with agent1"),
    ("agent1abc=ftp://127.0.0.1/submit", "http"),
    ("agent1abc=http://127.0.0.1:notaport/submit", "not a valid URL"),
    ("agent1abc=http://u:p@127.0.0.1:8005/submit", "user name"),
    ("agent1abc=http://10.0.0.5:8005/submit", "127.0.0.1 or localhost"),
    ("agent1abc=http://evil.example/submit", "127.0.0.1 or localhost"),
    ("agent1abc=http://127.0.0.1:1/s,agent1abc=http://127.0.0.1:2/s", "twice"),
])
def test_malformed_peers_refuse_to_start(entry, error, root, monkeypatch):
    with pytest.raises(ValueError, match=error):
        parse_peers(entry)
    monkeypatch.setenv("MARKET_SCOUT_PEERS", entry)
    monkeypatch.setenv("MARKET_SCOUT_SEED", "scout-test-seed-" + "x" * 32)
    with pytest.raises(ValueError, match="MARKET_SCOUT_PEERS"):
        make_agent(root, local_only=True)


def test_remote_peers_need_the_explicit_switch(root, monkeypatch):
    assert parse_peers("agent1abc=https://orch.example/submit", allow_remote=True) == {
        "agent1abc": "https://orch.example/submit"}
    monkeypatch.setenv("MARKET_SCOUT_PEERS", "agent1abc=https://orch.example/submit")
    monkeypatch.setenv("MARKET_SCOUT_PEERS_ALLOW_REMOTE", "1")
    assert resolver_rules(root)["agent1abc"] == "https://orch.example/submit"
    monkeypatch.setenv("MARKET_SCOUT_PEERS", f"{local_client_address(root)}=http://127.0.0.1:9/submit")
    with pytest.raises(ValueError, match="local demo client"):
        resolver_rules(root)
