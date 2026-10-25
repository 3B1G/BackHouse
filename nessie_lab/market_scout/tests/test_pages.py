"""The hardened fetcher and the snapshot cache, fully offline (httpx.MockTransport + an injected resolver)."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from conftest import PUBLIC, Resolver, store
from market_scout import pages
from market_scout.pages import FetchError, PageStore, check_url, page_text, pdf_text

HTML = {"content-type": "text/html; charset=utf-8"}


def ok(body="<p>Margherita 21</p>", headers=HTML, status=200):
    return lambda request: httpx.Response(status, headers=headers, content=body.encode() if isinstance(body, str) else body)


@pytest.mark.parametrize("url, msg", [
    ("ftp://a.example/menu", "only http and https"),
    ("file:///etc/passwd", "only http and https"),
    ("https://a.example:8443/menu", "port 8443"),
    ("http://a.example:22/", "port 22"),
    ("https://user:pw@a.example/menu", "user name or password"),
    ("https://user@a.example/menu", "user name or password"),
    ("https://www.doordash.com/store/x", "delivery-app"),
    ("https:///menu", "no host"),
])
def test_unsafe_urls_are_refused(url, msg):
    with pytest.raises(FetchError, match=msg):
        check_url(url)


def test_ports_80_and_443_are_allowed():
    assert check_url("https://a.example:443/m")[2] == 443
    assert check_url("http://a.example:80/m")[2] == 80
    assert check_url("http://a.example/m")[2] == 80


@pytest.mark.parametrize("addresses", [["10.0.0.5"], ["127.0.0.1"], [PUBLIC, "192.168.1.1"], ["169.254.169.254"],
                                       ["::1"], ["::ffff:127.0.0.1"], ["fd00::1"], ["100.64.0.1"], []])
def test_every_resolved_address_must_be_public(tmp_path, addresses):
    s = store(tmp_path, ok(), Resolver({"a.example": addresses}))
    with pytest.raises(FetchError, match="non-public|did not resolve"):
        s.get("https://a.example/menu")


def test_ip_literal_hosts_are_checked_too(tmp_path):
    with pytest.raises(FetchError, match="non-public"):
        store(tmp_path, ok()).get("http://127.0.0.1/menu")


def test_connection_is_pinned_to_the_checked_address(tmp_path):
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers["host"], request.extensions.get("sni_hostname")))
        return httpx.Response(200, headers=HTML, content=b"<p>Margherita 21</p>")

    snap = store(tmp_path, handler).get("https://a.example/menu")
    assert seen == [(PUBLIC, "a.example", "a.example")]
    assert "Margherita 21" in snap.text and len(snap.sha256) == 64
    assert snap.sha256 == __import__("hashlib").sha256(snap.html_path.read_bytes()).hexdigest()


def test_redirects_are_followed_by_hand_and_rechecked(tmp_path):
    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "https://b.example/new"})
        return httpx.Response(200, headers=HTML, content=b"<p>Tiramisu 12</p>")

    r = Resolver({"b.example": ["10.1.2.3"]})
    with pytest.raises(FetchError, match="non-public"):                # the second hop resolves privately
        store(tmp_path, handler, r).get("https://a.example/old")
    snap = store(tmp_path / "x", handler).get("https://a.example/old")
    assert "Tiramisu 12" in snap.text
    meta = json.loads(next((tmp_path / "x" / "pages").glob("*.json")).read_text())
    assert meta["final_url"] == "https://b.example/new"


def test_redirect_to_an_unsafe_scheme_or_port_is_refused(tmp_path):
    for location in ["file:///etc/passwd", "http://a.example:6379/", "https://u:p@a.example/"]:
        s = store(tmp_path, lambda req, loc=location: httpx.Response(302, headers={"location": loc}))
        with pytest.raises(FetchError):
            s.get("https://a.example/menu")


def test_at_most_three_redirects(tmp_path):
    def chain(n):
        def handler(request):
            hop = int(request.url.path.strip("/") or 0)
            if hop < n:
                return httpx.Response(302, headers={"location": f"/{hop + 1}"})
            return httpx.Response(200, headers=HTML, content=b"<p>Margherita 21</p>")
        return handler

    assert "Margherita" in store(tmp_path / "a", chain(3)).get("https://a.example/").text
    with pytest.raises(FetchError, match="more than 3 redirects"):
        store(tmp_path / "b", chain(4)).get("https://a.example/")


def test_http_errors_raise(tmp_path):
    with pytest.raises(FetchError, match="HTTP 403"):
        store(tmp_path, ok(status=403)).get("https://a.example/menu")


def test_oversize_content_length_is_refused_up_front(tmp_path):
    streamed = []

    def handler(request):
        def body():
            streamed.append(1)
            yield b"x"
        return httpx.Response(200, headers={**HTML, "content-length": str(6 * 1024 * 1024)}, content=body())

    with pytest.raises(FetchError, match="larger than 5 MB"):
        store(tmp_path, handler).get("https://a.example/menu")
    assert streamed == []


def test_streamed_body_is_capped(tmp_path):
    def handler(request):
        def body():
            for _ in range(100):
                yield b"x" * (64 * 1024)                  # 6.25 MB, no Content-Length
        return httpx.Response(200, headers=HTML, content=body())

    with pytest.raises(FetchError, match="larger than 5 MB"):
        store(tmp_path, handler).get("https://a.example/menu")


def test_one_deadline_covers_the_whole_fetch(tmp_path):
    t = [0.0]

    def handler(request):
        def body():
            for _ in range(10):
                t[0] += 3                                 # each chunk "takes" 3 s
                yield b"<p>x</p>"
        return httpx.Response(200, headers=HTML, content=body())

    with pytest.raises(FetchError, match="longer than 20 s"):
        store(tmp_path, handler, clock=lambda: t[0]).get("https://a.example/menu")

    t[0] = 0.0

    def slow_hops(request):                               # three hops of 8 s each: no single one is slow
        t[0] += 8
        nxt = {"/start": "/a", "/a": "/b"}.get(request.url.path)
        return httpx.Response(302, headers={"location": nxt}) if nxt else httpx.Response(200, headers=HTML, content=b"ok")

    with pytest.raises(FetchError, match="longer than 20 s"):
        store(tmp_path / "r", slow_hops, clock=lambda: t[0]).get("https://a.example/start")


@pytest.mark.parametrize("ctype", ["application/octet-stream", "text/plain", "image/png", "", "application/json"])
def test_only_html_and_pdf_content_types(tmp_path, ctype):
    with pytest.raises(FetchError, match="neither HTML nor PDF"):
        store(tmp_path, ok(headers={"content-type": ctype})).get("https://a.example/menu.pdf")


def test_pdf_needs_the_magic_bytes(tmp_path):
    s = store(tmp_path, ok(b"<html>not a pdf</html>", headers={"content-type": "application/pdf"}))
    with pytest.raises(FetchError, match="not one"):
        s.get("https://a.example/menu.pdf")


def test_pdf_page_and_text_caps(monkeypatch):
    """A real 50-page PDF (pdf_text walks the page tree itself), with each page's text stubbed."""
    import pypdf
    from test_redteam2 import text_pdf
    data = text_pdf(b"BT /F1 12 Tf 72 720 Td (x) Tj ET", pages_=50)
    order = {}
    calls = []

    def page_no(page):
        return order.setdefault(page.indirect_reference.idnum, len(order))
    monkeypatch.setattr(pypdf.PageObject, "extract_text",
                        lambda self, *a, **k: calls.append(page_no(self)) or f"Margherita {page_no(self)} " + "y" * 20_000)
    text = pdf_text(data)
    assert len(text) <= pages.MAX_PDF_TEXT
    assert max(calls) < pages.MAX_PDF_PAGES                 # stopped at the text cap, well inside 30 pages
    monkeypatch.setattr(pypdf.PageObject, "extract_text",
                        lambda self, *a, **k: calls.append(page_no(self)) or "Margherita 21 tomato basil")
    calls.clear()
    pdf_text(data)
    assert calls == list(range(30))


def test_html_is_capped_before_parsing():
    html = "<p>Margherita 21</p>" + "<p>" + "x" * (3 * 1024 * 1024) + "</p><p>Calzone 99</p>"
    text = page_text(html)
    assert "Margherita 21" in text and "Calzone 99" not in text


def test_hidden_elements_are_dropped():
    html = """<p>Margherita 21</p>
        <p hidden>Margherita 9</p>
        <div aria-hidden="true"><p>Tiramisu 3</p></div>
        <div aria-hidden="TRUE ">Tiramisu 4</div>
        <div style="color:red; DISPLAY : none">Diavola 5</div>
        <span style="visibility:hidden">Diavola 6</span>
        <template><p>Funghi Pizza 7</p></template>
        <noscript>Funghi Pizza 8</noscript>
        <div aria-hidden="false">Espresso 4</div>"""
    text = page_text(html)
    assert text.split("\n") == ["Margherita 21", "Espresso 4"]
    assert "Margherita 9" in page_text(html, include_hidden=True)     # closed menu tabs, for seed quotes only


def test_cached_snapshot_file_must_stay_in_the_pages_dir(tmp_path):
    s = store(tmp_path, ok())
    s.get("https://a.example/menu")
    meta_path = next((tmp_path / "pages").glob("*.json"))
    (tmp_path / "secret.html").write_text("<p>Margherita 1</p>")
    for bad in ["../secret.html", "..\\secret.html", str(tmp_path / "secret.html"), "C:secret.html", "", "..", None,
                "sub/x.html"]:
        meta = json.loads(meta_path.read_text())
        meta["file"] = bad
        meta_path.write_text(json.dumps(meta))
        with pytest.raises(FetchError, match="outside the pages directory"):
            PageStore(tmp_path / "pages", client=s.client, resolver=s.resolver).get("https://a.example/menu")


def test_cached_snapshot_is_used_offline_and_refresh_needs_six_hours(tmp_path):
    hits = []

    def handler(request):
        hits.append(1)
        return httpx.Response(200, headers=HTML, content=f"<p>Margherita {20 + len(hits)}</p>".encode())

    s = store(tmp_path, handler)
    assert "Margherita 21" in s.get("https://a.example/menu").text
    assert "Margherita 21" in s.get("https://a.example/menu").text
    assert "Margherita 21" in s.get("https://a.example/menu", refresh=True).text     # snapshot is minutes old
    assert len(hits) == 1
    meta_path = next((tmp_path / "pages").glob("*.json"))
    meta = json.loads(meta_path.read_text())
    meta["retrieved_at"] = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    meta_path.write_text(json.dumps(meta))
    assert "Margherita 22" in s.get("https://a.example/menu", refresh=True).text
    assert len(hits) == 2
    assert len(list((tmp_path / "pages").glob("*.html"))) == 2                      # the old bytes are kept


def test_resolver_failure_is_a_fetch_error(tmp_path):
    def broken(host, port):
        raise OSError("no network")

    with pytest.raises(FetchError, match="cannot resolve"):
        store(tmp_path, ok(), broken).get("https://a.example/menu")
