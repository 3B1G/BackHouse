"""Fetch competitor menu pages once and keep a snapshot, so every quoted price can be checked against the saved page
and the demo runs without the network (personal hotspots are banned at the venue).

The fetcher treats every URL as hostile (seed rows, Google Maps menu links and redirects all come from outside):
http/https on ports 80/443 only, no userinfo, every resolved address public, the connection pinned to the address
that was checked, redirects followed by hand and re-checked, no compression (Accept-Encoding: identity; any other
Content-Encoding is refused and the body is read raw, so a gzip bomb is never inflated), a 5 MB cap on the raw bytes,
one 20 s deadline for the whole fetch checked after every read, and 5 s per socket read.

Hidden text (a place to plant prices only a scraper sees) is dropped: hidden/aria-hidden, inline display:none,
visibility:hidden, font-size:0, opacity:0, off-screen positions and clipping, <style> rules that hide a simple .class
or #id, and closed <details> apart from the summary. Same-colour text (white on white) can't be detected reliably;
the defence against planted prices is analyze's trust rule (only seed competitors count), not this filter.
"""
import codecs
import hashlib
import ipaddress
import json
import logging
import re
import socket
import time
import unicodedata
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

# several restaurant sites answer 403 to non-browser clients, so present as a current desktop browser
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "identity"}
logging.getLogger("pypdf").setLevel(logging.ERROR)           # font-encoding warnings on every menu PDF
BLOCKED_HOSTS = ("doordash.", "ubereats.", "grubhub.", "postmates.", "seamless.", "caviar.")   # marked-up delivery prices

MAX_BYTES = 5 * 1024 * 1024          # body cap, streamed
MAX_HTML = 2 * 1024 * 1024           # HTML handed to BeautifulSoup
MAX_PDF_PAGES = 30                   # pages whose text is read
MAX_PDF_PAGE_COUNT = 200             # a menu PDF with more pages than this is refused before any extraction
MAX_PDF_TEXT = 200 * 1024            # extracted PDF text, characters
MAX_PDF_CONTENT = 512 * 1024         # decoded drawing operators on one PDF page (pypdf parses ~0.5 MB/s)
MAX_PDF_CONTENT_TOTAL = 2 * 1024 * 1024    # ... on all the pages read, together
PDF_DEADLINE = 8.0                   # seconds for one PDF's text, checked before every operator
DEADLINE = 20.0                      # seconds for the whole fetch, redirects included
READ_TIMEOUT = 5.0                   # seconds for any one socket read
MAX_REDIRECTS = 3
REFRESH_AGE = timedelta(hours=6)     # refresh=True re-fetches only a snapshot older than this
PORTS = (80, 443)
REDIRECTS = (301, 302, 303, 307, 308)
HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
# inline styles that hide text without display/visibility (matched against ";" + the style attribute)
_ZERO = r"(?:0+(?:\.0*)?|\.0+)\s?(?:px|em|rem|pt|%|vw|vh)?\s?(?:!important)?\s?$"
_SIDE = r"(?:left|top|right|bottom|text-indent|margin-left|margin-top)\s*:\s*-\s*"
HIDDEN_INLINE = re.compile(
    r"(?:^|;)\s*(?:font-size\s*:\s*" + _ZERO + r"|opacity\s*:\s*" + _ZERO
    + r"|" + _SIDE + r"\d{4,}(?:\.\d+)?\s*(?:px|pt)?(?![\w.])"
    + r"|" + _SIDE + r"\d{3,}(?:\.\d+)?\s*(?:em|rem|vw|vh|%)"
    + r"|clip\s?:\s?rect\(\s?0\w*(?:\s?,\s?|\s)0\w*(?:\s?,\s?|\s)0\w*(?:\s?,\s?|\s)0\w*\s?\)"
    + r"|clip-path\s?:\s?(?:inset\(\s?(?:50|[5-9]\d|100)(?:\.\d+)?%|circle\(\s?0[a-z%]*\s?(?:at [^)]*)?\)))", re.I)
# CSS is parsed by hand (str.find / split / partition), never by a regex over the whole block: a backtracking rule
# regex is quadratic on a <style> with no "{" (40 KB took 10 s). Each block, and all blocks together, are capped.
MAX_CSS = 64 * 1024                  # characters of CSS read per <style> block, and in total per page
MAX_DECL = 200                       # characters of one declaration (after collapsing whitespace) the hide checks see
SHOWS = re.compile(r"display ?: ?(?!none)[a-z-]+|visibility ?: ?visible", re.I)    # one declaration, collapsed
SIMPLE_SELECTOR = re.compile(r"^([.#])(-?[_a-zA-Z][\w-]*)$")


class FetchError(ValueError):
    """The URL or the response broke a fetch rule; the page is skipped and noted as a limitation."""


class Snapshot:
    """A saved page: raw bytes on disk plus the visible text the extractors and checks read."""

    def __init__(self, url, html_path: Path | None, text: str, retrieved_at: datetime, sha256: str | None = None,
                 all_text: str | None = None, final_url: str | None = None):
        self.url, self.html_path, self.text, self.retrieved_at = url, html_path, text, retrieved_at
        # final_url: where the page was really read, after redirects; it is the source_url of every price read off
        # it, and trust is judged on it (a seed site that redirects elsewhere does not lend the target its trust).
        self.final_url = url if final_url is None else final_url
        # text: what a visitor sees, read by the rule and ASI readers and used to check their quotes.
        # all_text: also the hidden parts (closed menu tabs); only hand-collected seed quotes are checked against it.
        self.all_text = text if all_text is None else all_text
        if sha256 is None and html_path is not None and Path(html_path).exists():
            sha256 = hashlib.sha256(Path(html_path).read_bytes()).hexdigest()
        self.sha256 = sha256                              # of the saved raw file, recorded with every check


# ---------------------------------------------------------------- page text
def _hides(declarations: str) -> bool:
    """A style attribute or rule body hides its element. Checked one declaration at a time, whitespace collapsed and
    each cut to MAX_DECL characters, so no pattern ever backtracks over a long run (linear in the input)."""
    for decl in declarations.split(";"):
        decl = " ".join(decl.split())[:MAX_DECL]
        if decl and (HIDDEN_STYLE.search(decl) or HIDDEN_INLINE.match(decl)):
            return True
    return False


def strip_comments(css: str) -> str:
    """CSS without /* ... */ comments (an unclosed one runs to the end, as in a browser). A str.find loop: linear."""
    out, i = [], 0
    while True:
        j = css.find("/*", i)
        if j < 0:
            out.append(css[i:])
            break
        out.append(css[i:j])
        out.append(" ")
        k = css.find("*/", j + 2)
        if k < 0:
            break
        i = k + 2
    return "".join(out)


def css_rules(css: str):
    """(selectors, body) of every `selectors { body }`: split on "}", then the last "{" of each piece. The selector
    is the text since the previous brace (so rules inside @media count), as the old rule regex read it. Linear."""
    for chunk in css.split("}")[:-1]:                   # the text after the last "}" is no complete rule
        selectors, brace, body = chunk.rpartition("{")
        if not brace:
            continue
        selectors = selectors.rpartition("{")[2]
        if selectors.strip():
            yield selectors, body


def hidden_selectors(css: str) -> tuple[set[str], set[str]]:
    """-> (classes, ids) that a <style> rule hides with a simple `.cls` or `#id` selector. A class that some other
    rule shows again (`.tab.active { display: block }`) is left visible: tabbed menus work that way."""
    css = strip_comments(css[:MAX_CSS])
    hide, shown = {".": set(), "#": set()}, {".": set(), "#": set()}
    for selectors, body in css_rules(css):
        hides = _hides(body)
        shows = any(SHOWS.match(" ".join(d.split())[:MAX_DECL]) for d in body.split(";"))
        for sel in selectors.split(","):
            sel = sel.strip()
            m = SIMPLE_SELECTOR.match(sel)
            if hides and m:
                hide[m.group(1)].add(m.group(2))
            elif shows and not hides:
                for kind, name in re.findall(r"([.#])(-?[_a-zA-Z][\w-]*)", sel):
                    shown[kind].add(name)
    return hide["."] - shown["."], hide["#"] - shown["#"]


def _hidden(tag, classes=frozenset(), ids=frozenset()) -> bool:
    attrs = getattr(tag, "attrs", None) or {}
    if "hidden" in attrs:
        return True
    if str(attrs.get("aria-hidden", "")).strip().lower() == "true":
        return True
    if _hides(str(attrs.get("style", ""))):
        return True
    cls = attrs.get("class") or []
    cls = cls.split() if isinstance(cls, str) else cls
    return bool(classes.intersection(cls)) or str(attrs.get("id", "")) in ids


def close_tags(html: str) -> str:
    """HTML that Python's html.parser reads in linear time. On a "<" whose tag never closes ("<a <a <a ...") it
    rescans to the end of the input for every one: 20 KB took 13 s. A "<" with another "<" before the next ">", or
    with no ">" after it, is not a tag and becomes "&lt;" (shown as text); an unclosed comment runs to the end and is
    dropped. One left-to-right pass with str.find."""
    out, i, n = [], 0, len(html)
    while i < n:
        j = html.find("<", i)
        if j < 0:
            out.append(html[i:])
            break
        out.append(html[i:j])
        if html.startswith("<!--", j):
            k = html.find("-->", j + 4)
            if k < 0:
                break                                     # unclosed comment: the rest is comment
            out.append(html[j:k + 3])
            i = k + 3
            continue
        close, reopen = html.find(">", j + 1), html.find("<", j + 1)
        if close < 0:                                     # no tag closes from here on
            out.append(html[j:].replace("<", "&lt;"))
            break
        if 0 <= reopen < close:                           # "<" ... "<" ... ">": the first is text
            out.append("&lt;")
            i = j + 1
            continue
        out.append(html[j:close + 1])
        i = close + 1
    return "".join(out)


def page_text(html: str, include_hidden=False) -> str:
    """Visible text only: scripts, templates and anything hidden (a place to plant fake prices) are dropped.
    include_hidden keeps hidden elements and templates (tabbed menus hide every tab but the first with display:none)."""
    soup = BeautifulSoup(close_tags(html[:MAX_HTML]), "html.parser")
    classes, ids = set(), set()
    if not include_hidden:
        budget = MAX_CSS                                  # CSS read for the whole page
        for style in soup.find_all("style"):
            if budget <= 0:
                break
            css = style.get_text()[:min(MAX_CSS, budget)]
            budget -= len(css)
            c, i = hidden_selectors(css)
            classes |= c
            ids |= i
    for tag in soup(["script", "style", "noscript", "svg"] + ([] if include_hidden else ["template"])):
        if not tag.decomposed:                            # nested <svg>/<template>: each subtree removed once
            tag.decompose()
    if not include_hidden:
        for tag in soup.find_all(lambda t: _hidden(t, classes, ids)):
            if not tag.decomposed:                        # a hidden child of a hidden parent is already gone
                tag.decompose()
        for details in soup.find_all("details"):          # closed <details>: only the summary shows
            if details.decomposed or "open" in (details.attrs or {}):
                continue
            for child in list(details.children):
                if getattr(child, "name", None) != "summary":
                    child.decompose() if hasattr(child, "decompose") else child.extract()   # decompose: nested
                    # <details> inside are marked decomposed and skipped, so deep nesting stays linear
    lines = (re.sub(r"\s+", " ", line).strip() for line in soup.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


INHERITED = ("/Resources", "/MediaBox", "/CropBox", "/Rotate")


def _pdf_pages(reader, limit):
    """The first `limit` pages, walking the page tree ourselves: pypdf's reader.pages flattens the whole tree first,
    and a 5 MB file can hold ~40,000 pages whatever its /Count says. Loops and deep trees are cut off."""
    from pypdf import PageObject
    from pypdf.generic import DictionaryObject

    stack, seen, out = [(reader.trailer["/Root"].get_object()["/Pages"], {}, 0)], set(), []
    while stack and len(out) < limit:
        ref, inherit, depth = stack.pop()
        node = ref.get_object()
        key = getattr(getattr(node, "indirect_reference", None), "idnum", None) or id(node)
        if key in seen or depth > 32 or not isinstance(node, DictionaryObject):
            continue
        seen.add(key)
        if "/Kids" in node:
            inherit = {**inherit, **{k: node[k] for k in INHERITED if k in node}}
            kids = node["/Kids"].get_object()
            kids = list(kids)[:MAX_PDF_PAGE_COUNT] if isinstance(kids, list) else []
            stack.extend((kid, inherit, depth + 1) for kid in reversed(kids))
            continue
        page = PageObject(reader, node.indirect_reference)
        page.update(node)
        for k, v in inherit.items():
            if k not in page:
                page[k] = v
        out.append(page)
    return out


def _decoded_size(stream, cap: int) -> int:
    """How many bytes a stream decodes to, counted without inflating more than cap + 1 (a 5 MB Flate stream can
    hold gigabytes). A filter other than none or Flate counts as too big: content streams don't use them."""
    from pypdf.generic import StreamObject

    stream = stream.get_object()
    if not isinstance(stream, StreamObject):
        return 0
    filters = stream.get("/Filter")
    filters = [] if filters is None else list(filters.get_object()) if isinstance(filters.get_object(), list) \
        else [filters.get_object()]
    raw = stream._data
    if not filters:
        return len(raw)
    if [str(f) for f in filters] not in (["/FlateDecode"], ["/Fl"]):
        return cap + 1
    try:
        return len(zlib.decompressobj().decompress(raw, cap + 1))
    except zlib.error:
        return cap + 1


def _content_size(page, cap: int) -> int:
    """Decoded bytes of drawing operators extract_text would parse on this page: its /Contents plus every form
    XObject it can reach (nested forms included), stopping once the count passes cap."""
    from pypdf.generic import DictionaryObject

    total = 0
    contents = page.get("/Contents")
    contents = contents.get_object() if contents is not None else None
    for s in (list(contents) if isinstance(contents, list) else [contents] if contents is not None else []):
        total += _decoded_size(s, cap - total)
        if total > cap:
            return total
    todo, seen = [page.get("/Resources")], set()
    while todo and len(seen) < 1000:
        res = todo.pop()
        res = res.get_object() if res is not None else None
        if not isinstance(res, DictionaryObject):
            continue
        xobjects = res.get("/XObject")
        xobjects = xobjects.get_object() if xobjects is not None else None
        if not isinstance(xobjects, DictionaryObject):
            continue
        for ref in xobjects.values():
            form = ref.get_object()
            key = getattr(getattr(form, "indirect_reference", None), "idnum", None) or id(form)
            if key in seen:
                continue
            seen.add(key)
            if not isinstance(form, DictionaryObject) or form.get("/Subtype") != "/Form":
                continue
            total += _decoded_size(form, cap - total)
            if total > cap:
                return total
            todo.append(form.get("/Resources"))
    return total


class _PdfTimeout(Exception):
    pass


def pdf_text(data: bytes) -> str:
    """Text layer of a PDF menu (first MAX_PDF_PAGES pages). Image-only PDFs have none and are rejected (OCR).
    Bounded however the file lies: /Count over MAX_PDF_PAGE_COUNT is refused, only the first MAX_PDF_PAGES pages of
    the real tree are visited, a page whose drawing operators decode to more than MAX_PDF_CONTENT is skipped (pypdf
    parses them in Python), all pages together get MAX_PDF_CONTENT_TOTAL and PDF_DEADLINE seconds, and the text is
    cut at MAX_PDF_TEXT characters."""
    import io

    from pypdf import PdfReader

    # plain mode: on the seed menus it verifies more quotes than layout mode (51 vs 48 of 57). Two-column menus
    # (North Beach Restaurant) interleave in both modes, so their quotes fail verification and are left out.
    deadline = time.monotonic() + PDF_DEADLINE
    reader = PdfReader(io.BytesIO(data))
    try:                                                  # the page tree's own count: no walk over every page
        count = int(reader.trailer["/Root"]["/Pages"]["/Count"])
    except Exception:
        count = None
    if count is None or not 0 < count <= MAX_PDF_PAGE_COUNT:
        raise ValueError(f"PDF has {count if count is not None else 'an unreadable number of'} pages "
                         f"(a menu has at most {MAX_PDF_PAGE_COUNT})")

    def watch(*_):                                        # called by extract_text before every operator
        if time.monotonic() > deadline:
            raise _PdfTimeout

    parts, size, content, skipped = [], 0, 0, 0
    for page in _pdf_pages(reader, min(count, MAX_PDF_PAGES)):
        if time.monotonic() > deadline:
            break
        n = _content_size(page, MAX_PDF_CONTENT)
        if n > MAX_PDF_CONTENT or content + n > MAX_PDF_CONTENT_TOTAL:
            skipped += 1
            continue
        content += n
        try:
            part = page.extract_text(visitor_operand_before=watch) or ""
        except _PdfTimeout:
            break
        parts.append(part[:MAX_PDF_TEXT - size])
        size += len(parts[-1])
        if size >= MAX_PDF_TEXT:
            break
    text = unicodedata.normalize("NFKC", "\n".join(parts))[:MAX_PDF_TEXT]    # ligatures ("ﬀ" = "ff") back to letters
    lines = (re.sub(r"\s+", " ", line).strip() for line in text.splitlines())
    text = "\n".join(line for line in lines if line)
    if len(text) < 40:
        if skipped:
            raise ValueError(f"PDF menu pages are too large to read ({skipped} over "
                             f"{MAX_PDF_CONTENT // 1024} KB of drawing operators)")
        raise ValueError("PDF menu has no text layer (image-only)")
    return text


def _codec(encoding) -> str:
    try:
        return codecs.lookup(encoding or "utf-8").name
    except (LookupError, TypeError):
        return "utf-8"


def _texts(kind, data: bytes, encoding=None) -> tuple[str, str]:
    """-> (visible text, text including hidden elements)."""
    if kind == "pdf":
        text = pdf_text(data)
        return text, text
    html = data[:MAX_HTML].decode(_codec(encoding), "replace")
    return page_text(html), page_text(html, include_hidden=True)


def normalize(text: str) -> str:
    """For quote checks: case, whitespace, dollar signs and dash styles don't matter; the words and digits do."""
    text = unicodedata.normalize("NFKC", text).lower().replace("–", "-").replace("—", "-").replace("$", " ")
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------- URL and address checks
def system_resolver(host: str, port: int) -> list[str]:
    return [ai[4][0] for ai in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def check_url(url: str):
    """-> (parts, host, port) for an http(s) URL on port 80/443 without userinfo, else FetchError."""
    if not isinstance(url, str) or len(url) > 2000:
        raise FetchError("not a usable URL")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchError(f"only http and https are fetched, not {parts.scheme or 'no scheme'!r}")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise FetchError("URLs with a user name or password are not fetched")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise FetchError("URL has no host")
    try:
        port = parts.port
    except ValueError:
        raise FetchError("URL has an invalid port") from None
    if port is not None and port not in PORTS:
        raise FetchError(f"port {port} is not fetched (80 and 443 only)")
    if any(h in host.lower() for h in BLOCKED_HOSTS):
        raise FetchError(f"delivery-app prices are marked up; not used: {host}")
    return parts, host, port or (443 if parts.scheme == "https" else 80)


MAX_SOURCE_URL = 500


def source_url(url) -> str | None:
    """url if it may be stored as a price's source (http(s), at most MAX_SOURCE_URL characters), else None."""
    if not isinstance(url, str) or not url or len(url) > MAX_SOURCE_URL:
        return None
    try:
        check_url(url)
    except FetchError:
        return None
    return url


NAT64 = ipaddress.ip_network("64:ff9b::/96")
REFUSED_V6 = (ipaddress.ip_network("::/96"),             # IPv4-compatible (deprecated)
              ipaddress.ip_network("64:ff9b:1::/48"))    # local-use NAT64


def _is_public(ip) -> bool:
    """Public, and so is any IPv4 address it carries (mapped ::ffff:0:0/96, NAT64 64:ff9b::/96, 6to4, Teredo).
    IPv4-compatible ::/96 and local-use NAT64 64:ff9b:1::/48 are refused outright."""
    if not ip.is_global:
        return False
    if ip.version == 6:
        if any(ip in net for net in REFUSED_V6):
            return False
        embedded = [ip.ipv4_mapped, ip.sixtofour, ip.teredo[1] if ip.teredo else None]
        if ip in NAT64:
            embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if any(v4 is not None and not v4.is_global for v4 in embedded):
            return False
    return True


def public_address(host: str, port: int, resolver) -> str:
    """The address to connect to. Every address the name resolves to must be public, not just the first."""
    try:
        addresses = [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        try:
            raw = resolver(host, port)
        except OSError as exc:
            raise FetchError(f"cannot resolve {host}: {exc}") from None
        try:
            addresses = [ipaddress.ip_address(str(a).split("%")[0]) for a in raw]
        except ValueError:
            raise FetchError(f"{host} resolved to something that is not an IP address") from None
    if not addresses:
        raise FetchError(f"{host} did not resolve")
    bad = [str(a) for a in addresses if not _is_public(a)]
    if bad:
        raise FetchError(f"{host} resolves to a non-public address ({bad[0]})")
    return str(addresses[0])


# ---------------------------------------------------------------- the store
class PageStore:
    def __init__(self, directory: Path, client: httpx.Client | None = None, resolver=None, clock=time.monotonic):
        self.dir = Path(directory)
        self.client = client
        self.resolver = resolver or system_resolver
        self.clock = clock

    def _paths(self, url):
        key = hashlib.sha256(url.encode()).hexdigest()[:16]
        return self.dir / f"{key}.json", key

    def _raw_path(self, name) -> Path:
        """A cached snapshot's file must be a bare file name inside the pages directory."""
        if (not isinstance(name, str) or not name or name in (".", "..") or any(c in name for c in "/\\:\0")
                or Path(name).name != name):
            raise FetchError("snapshot metadata names a file outside the pages directory")
        path = self.dir / name
        if path.resolve().parent != self.dir.resolve():
            raise FetchError("snapshot metadata names a file outside the pages directory")
        return path

    def _cached(self, url, meta_path) -> Snapshot:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("kind") not in ("html", "pdf"):
            raise FetchError("snapshot metadata has an unknown kind")
        raw = self._raw_path(meta.get("file"))
        data = raw.read_bytes()
        text, all_text = _texts(meta["kind"], data, meta.get("encoding"))
        final = meta.get("final_url") or url              # older snapshots have no final_url: the URL asked for
        if source_url(final) is None:
            raise FetchError("snapshot metadata has an unusable final URL")
        return Snapshot(url, raw, text, datetime.fromisoformat(meta["retrieved_at"]), hashlib.sha256(data).hexdigest(),
                        all_text, final_url=final)

    def get(self, url: str, refresh=False) -> Snapshot:
        """Saved snapshot unless refresh and it is older than REFRESH_AGE. HTML and text PDFs; raises on delivery
        apps, unsafe URLs, HTTP errors, oversize or unexpected content and image-only menus."""
        check_url(url)
        meta_path, key = self._paths(url)
        if meta_path.exists():
            snap = self._cached(url, meta_path)
            age = datetime.now(timezone.utc) - snap.retrieved_at.astimezone(timezone.utc)
            if not refresh or age < REFRESH_AGE:
                return snap
        data, kind, encoding, final_url, status = self.fetch(url)
        if source_url(final_url) is None:                  # it is stored as every price's source: same URL filter
            raise FetchError(f"redirected to a URL that can't be a price's source (over {MAX_SOURCE_URL} characters)")
        text, all_text = _texts(kind, data, encoding)     # same decoding path as a cached read
        now = datetime.now(timezone.utc)
        self.dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(data).hexdigest()
        # content-addressed: a refresh adds a file and never replaces the bytes an earlier report quoted
        raw = self.dir / f"{key}-{digest[:12]}.{kind}"
        if not raw.exists():
            raw.write_bytes(data)
        meta_path.write_text(json.dumps({"url": url, "final_url": final_url, "status": status, "kind": kind,
                                         "encoding": encoding, "file": raw.name, "retrieved_at": now.isoformat()}),
                             encoding="utf-8")
        return Snapshot(url, raw, text, now, digest, all_text, final_url=final_url)

    def fetch(self, url: str):
        """-> (body, kind, encoding, final_url, status). Every hop is checked; the whole fetch has one deadline."""
        deadline = self.clock() + DEADLINE
        client = self.client or httpx.Client(headers=HEADERS, follow_redirects=False, trust_env=False)
        try:
            current = url
            for hop in range(MAX_REDIRECTS + 1):
                parts, host, port = check_url(current)
                address = public_address(host, port, self.resolver)
                response = self._send(client, parts, host, port, address, deadline)
                try:
                    if response.status_code in REDIRECTS:
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError(f"HTTP {response.status_code} without a Location header")
                        if hop == MAX_REDIRECTS:
                            raise FetchError(f"more than {MAX_REDIRECTS} redirects")
                        current = urljoin(current, location)
                        continue
                    if not 200 <= response.status_code < 300:
                        raise FetchError(f"HTTP {response.status_code}")
                    kind, encoding = self._kind(response)
                    data = self._body(response, deadline)
                    if kind == "pdf" and not data.startswith(b"%PDF-"):
                        raise FetchError("served as a PDF but the body is not one")
                    return data, kind, encoding, current, response.status_code
                finally:
                    response.close()
        finally:
            if self.client is None:
                client.close()

    def _remaining(self, deadline) -> float:
        left = deadline - self.clock()
        if left <= 0:
            raise FetchError(f"fetch took longer than {DEADLINE:.0f} s")
        return left

    def _send(self, client, parts, host, port, address, deadline):
        """Connect to the checked address (not a second lookup that could answer differently), with the real name in
        the Host header and, for https, in SNI and certificate checks."""
        ip_host = f"[{address}]" if ":" in address else address
        target = parts._replace(netloc=f"{ip_host}:{port}").geturl()
        default = (parts.scheme == "https" and port == 443) or (parts.scheme == "http" and port == 80)
        headers = {**HEADERS, "Host": host if default else f"{host}:{port}"}
        extensions = {"sni_hostname": host} if parts.scheme == "https" else {}
        left = self._remaining(deadline)
        timeout = httpx.Timeout(min(left, 10.0), read=min(left, READ_TIMEOUT))
        request = client.build_request("GET", target, headers=headers, timeout=timeout, extensions=extensions)
        try:
            return client.send(request, stream=True, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise FetchError(f"{type(exc).__name__} fetching {host}") from None

    @staticmethod
    def _kind(response):
        ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype == "text/html":
            kind = "html"
        elif ctype == "application/pdf":
            kind = "pdf"
        else:
            raise FetchError(f"menu is neither HTML nor PDF ({ctype or 'unknown type'})")
        coding = response.headers.get("content-encoding", "").strip().lower()
        if coding not in ("", "identity"):                # we asked for identity; never inflate what we didn't ask for
            raise FetchError(f"compressed response (Content-Encoding: {coding[:40]}) refused")
        length = response.headers.get("content-length")
        if length is not None:
            try:
                too_big = int(length) > MAX_BYTES
            except ValueError:
                raise FetchError("invalid Content-Length") from None
            if too_big:
                raise FetchError(f"page is larger than {MAX_BYTES // (1024 * 1024)} MB")
        return kind, _codec(response.charset_encoding)

    def _body(self, response, deadline) -> bytes:
        """The raw body (Content-Encoding was checked to be identity), capped, with the deadline checked after every
        read. A response that arrived already read (an in-memory test response) is taken as it is, under the cap."""
        if response.is_stream_consumed:
            if len(response.content) > MAX_BYTES:
                raise FetchError(f"page is larger than {MAX_BYTES // (1024 * 1024)} MB")
            self._remaining(deadline)
            return response.content
        chunks, size = [], 0
        try:
            for chunk in response.iter_raw():         # raw: never decompressed; one chunk per network read
                size += len(chunk)
                if size > MAX_BYTES:
                    raise FetchError(f"page is larger than {MAX_BYTES // (1024 * 1024)} MB")
                chunks.append(chunk)
                self._remaining(deadline)
        except httpx.HTTPError as exc:
            raise FetchError(f"{type(exc).__name__} while reading the page") from None
        self._remaining(deadline)
        return b"".join(chunks)
