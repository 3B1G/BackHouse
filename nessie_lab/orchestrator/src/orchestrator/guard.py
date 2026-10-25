"""Every figure in an answer must come from a tool result.

normalize(text) first: terminal escapes removed, NFKC, format/control characters (zero-width spaces, soft hyphens,
word joiners, ...) dropped, every Unicode dash and minus made "-", every Unicode digit made ASCII. turn.py guards
exactly the normalised string it sends.

check(text, session) finds every figure in the answer and looks for it in the tool results in session.log.
Returns (ok, unsupported). Figures:
- money: $1,234.56, $ 21, -$40, US$5, $12k, $1.2 million, USD 5,000, 48,000 USD, 48 thousand, 20 dollars,
  "twelve thousand dollars", "a hundred bucks"; money written in words that can't be parsed is always unsupported;
- percents: 14%, -6.5 %, 14 percent, 14 pct, "ninety nine percent";
- dates: 2026-10-30, 10/30/2026 (US order), Oct 31, October 31, 2026, 31 October 2026. A date without a year
  matches any tool date on that month and day.

What counts as support (only from successful results of the orchestrator's own tools; error results, peer replies
and anything else in the log are ignored):
- numbers: under a key that mentions "cents" a number is cents (1800 -> $18.00); any other number is taken as itself
  (a dollar amount or a percent), and a fraction between -1 and 1 also as a percent (0.142 -> 14.2%);
- dates: date values, and strings that are exactly an ISO date or timestamp, outside free-text keys;
- the display strings fmt.py makes from cents (`*_usd`, `*_display`).
Free text never counts: memos, payees, titles, questions, scenario names and labels, dish names, error messages and
anything the caller sent (the `args`, `scenario`, `request` and `payload` subtrees are skipped whole). So a model can
not launder "$48,000" by putting it in a tool argument and reading it back.
Signs are ignored (a "-$40" effect supports "saves $40"). Rounding follows the answer: "$21" matches 21.00 or 20.75
(whole dollars), "$21.00" needs 21.00 exactly, "14%" matches 14.28. Figures the model derived itself (sums,
differences) are unsupported on purpose."""
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

TOOL_NAMES = frozenset(("get_cash_position", "list_bills_due", "lookup", "get_findings", "scan_market", "get_menu",
                        "list_upcoming_changes", "forecast_base", "forecast_scenario", "propose_fix",
                        "preview_suggestion"))
ECHO_KEYS = frozenset(("args", "scenario", "request", "payload"))          # caller input echoed back: never support
TEXT_KEYS = frozenset(("name", "label", "title", "question", "memo", "note", "notes", "explanation", "description",
                       "nickname", "payee", "doc", "why", "error", "target", "item", "our_item", "dish", "based_on",
                       "assumptions", "limitations", "restaurants", "query", "planned_change", "detail", "text"))

ESCAPES = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]|(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?"
                     r"|\x1b[ -/]*[0-~]")
DROP = frozenset(("Cc", "Cf", "Cs", "Co", "Cn"))
DASHES = {c: "-" for c in "‐‑‒–—―−﹘﹣－⸺⸻"}
SEPARATORS = {"٫": ".", "٬": ","}                              # Arabic decimal and thousands separators


def normalize(text) -> str:
    """The one cleaning pass, run on the answer before it is guarded and sent. Newlines and tabs are kept."""
    text = ESCAPES.sub("", str(text or ""))
    try:
        text = unicodedata.normalize("NFKC", text)
    except UnicodeError:
        pass
    out = []
    for ch in text:
        if ch in "\n\t":
            out.append(ch)
            continue
        cat = unicodedata.category(ch)
        if cat in DROP:
            continue
        if ch in DASHES:
            out.append("-")
        elif ch in SEPARATORS:
            out.append(SEPARATORS[ch])
        elif cat == "Nd" and not ("0" <= ch <= "9"):
            out.append(str(unicodedata.decimal(ch, 0)))
        else:
            out.append(ch)
    return "".join(out)


_norm = normalize


# ---------------------------------------------------------------- finding figures in text
NUM = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)"
SCALE_WORDS = {"k": 1000, "thousand": 1000, "m": 1_000_000, "mn": 1_000_000, "million": 1_000_000,
               "bn": 1_000_000_000, "b": 1_000_000_000, "billion": 1_000_000_000, "hundred": 100}
SCALE = r"(?:[kKmM]|mn|bn|(?i:thousand|million|billion))\b"
TAIL = r"(?![\w.]\d)(?![a-zA-Z])"
MONEY_PATTERNS = [
    re.compile(rf"(?<![\w$])-?(?:US)?\$\s?-?(?P<num>{NUM})(?:\s?(?P<scale>{SCALE}))?{TAIL}"),
    re.compile(rf"(?<![\w])(?:USD|(?i:US\s+dollars?))\s?\$?\s?(?P<num>{NUM})(?:\s?(?P<scale>{SCALE}))?{TAIL}"),
    re.compile(rf"(?<![\w$.,])(?P<num>{NUM})(?:\s?(?P<scale>{SCALE}))?\s?(?:USD\b|(?i:dollars?|bucks)\b)"),
    re.compile(rf"(?<![\w$.,])(?P<num>{NUM})\s?(?P<scale>(?i:thousand|million|billion|hundred))\b"),
]
PERCENT = re.compile(rf"(?<![\w.])-?(?P<num>{NUM})\s?(?:%|(?i:percent|per\s?cent|pct)\b)")

UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                    "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if i > 1}
BIG = {"thousand": 1000, "million": 1_000_000, "billion": 1_000_000_000}
NUMBER_WORD = r"(?:" + "|".join(sorted([*UNITS, *TENS, "hundred", *BIG, "a"], key=len, reverse=True)) + r")"
WORDS = re.compile(rf"(?i)\b(?P<words>{NUMBER_WORD}(?:(?:[\s-]+and)?[\s-]+{NUMBER_WORD})*)\b"
                   r"(?P<unit>\s+(?:US\s+)?(?:dollars?|bucks|USD|percent|per\s?cent|pct)\b)?")


def parse_words(text: str) -> Decimal | None:
    """'twelve thousand four hundred and five' -> 12405; None when it isn't a well-formed number."""
    tokens = [t for t in re.split(r"[\s-]+", text.lower()) if t and t != "and"]
    total, group, last_big, seen = 0, 0, None, False
    for i, t in enumerate(tokens):
        if t == "a":
            following = tokens[i + 1] if i + 1 < len(tokens) else None
            if group or seen or following not in (None, "hundred", *BIG):
                return None
            group = 1
        elif t in UNITS:
            rest = group % 100
            if not (rest == 0 or (rest >= 20 and rest % 10 == 0 and UNITS[t] < 10)):
                return None                                  # "five six", "twelve three"
            group += UNITS[t]
        elif t in TENS:
            if group % 100:
                return None                                  # "twenty thirty"
            group += TENS[t]
        elif t == "hundred":
            if group == 0 or group >= 100:
                return None
            group *= 100
        elif t in BIG:
            if group == 0 or (last_big is not None and BIG[t] >= last_big):
                return None
            total, group, last_big = total + group * BIG[t], 0, BIG[t]
        else:
            return None
        seen = True
    return Decimal(total + group) if seen else None


def _dec(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def _gran(num: str) -> Decimal:
    return Decimal(1).scaleb(-len(num.split(".")[1])) if "." in num else Decimal(1)


MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
          "nov": 11, "dec": 12}
MONTH = (r"(?:(?i:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|june?|july?|aug(?:ust)?|sept?(?:ember)?|"
         r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)|May|MAY)")
ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
SLASH_DATE = re.compile(r"(?<![\d/.])(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})(?![\d/])")
MONTH_DAY = re.compile(rf"\b(?P<mon>{MONTH})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(?P<year>\d{{4}})\b)?")
DAY_MONTH = re.compile(rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<mon>{MONTH})\b\.?(?:,?\s+(?P<year>\d{{4}})\b)?")


def _iso(year, month, day) -> str | None:
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except (TypeError, ValueError):
        return None


def _scan(text: str) -> list[tuple[int, int, str, str, object, Decimal | None]]:
    """[(start, end, kind, as written, value, granularity)] in order, overlaps dropped (earlier and longer wins).
    kind is "money", "percent" or "date"; value is Decimal dollars/percent (None = unparseable, never supported) or a
    date key: "YYYY-MM-DD", "--MM-DD" (no year), or the text as written for an impossible date."""
    text = normalize(text)
    found = []
    for pattern in MONEY_PATTERNS:
        for m in pattern.finditer(text):
            num, scale = m.group("num"), (m.group("scale") or "").lower()
            value = _dec(num)
            mult = Decimal(SCALE_WORDS.get(scale, 1))
            found.append((m.start(), m.end(), "money", m.group(0).strip(),
                          None if value is None else abs(value * mult), _gran(num) * mult))
    for m in PERCENT.finditer(text):
        value = _dec(m.group("num"))
        found.append((m.start(), m.end(), "percent", m.group(0).strip(),
                      None if value is None else abs(value), _gran(m.group("num"))))
    for m in WORDS.finditer(text):
        words, unit = m.group("words"), (m.group("unit") or "").strip().lower()
        has_scale = re.search(r"(?i)\b(?:hundred|thousand|million|billion)\b", words)
        if not unit and not has_scale:
            continue                                          # "one of the dishes"
        if not unit and words.strip().lower() in ("a", "and"):
            continue
        value = parse_words(words)
        kind = "percent" if re.match(r"(?:percent|per\s?cent|pct)", unit) else "money"
        lowest = Decimal(1)
        for w, n in (("hundred", 100), ("thousand", 1000), ("million", 1_000_000), ("billion", 1_000_000_000)):
            if re.search(rf"(?i)\b{w}\s*$", words):
                lowest = Decimal(n)
        found.append((m.start(), m.end(), kind, m.group(0).strip(), value, lowest))
    for m in ISO_DATE.finditer(text):
        found.append((m.start(), m.end(), "date", m.group(0), _iso(*m.groups()) or m.group(0), None))
    for m in SLASH_DATE.finditer(text):
        a, b, y = m.groups()
        year = int(y) + (2000 if len(y) == 2 else 0)
        found.append((m.start(), m.end(), "date", m.group(0), _iso(year, a, b) or _iso(year, b, a) or m.group(0), None))
    for pattern in (MONTH_DAY, DAY_MONTH):
        for m in pattern.finditer(text):
            month = MONTHS[m.group("mon").lower()[:3]]
            day, year = int(m.group("day")), m.group("year")
            if year:
                key = _iso(year, month, day) or m.group(0)
            else:
                key = f"--{month:02d}-{day:02d}" if _iso(2028, month, day) else m.group(0)
            found.append((m.start(), m.end(), "date", m.group(0).strip(), key, None))
    found.sort(key=lambda f: (f[0], -(f[1] - f[0])))
    out, end = [], -1
    for f in found:
        if f[0] >= end:
            out.append(f)
            end = f[1]
    return out


def money_figures(text: str) -> list[tuple[str, Decimal | None, Decimal]]:
    """[(as written, abs value in dollars or None, granularity)]"""
    return [(raw, value, gran) for _, _, kind, raw, value, gran in _scan(text) if kind == "money"]


def percent_figures(text: str) -> list[tuple[str, Decimal | None, Decimal]]:
    return [(raw, value, gran) for _, _, kind, raw, value, gran in _scan(text) if kind == "percent"]


def date_figures(text: str) -> list[str]:
    """Date keys: 'YYYY-MM-DD', '--MM-DD' for a date written without a year, or the text of an impossible date."""
    return [value for _, _, kind, _, value, _ in _scan(text) if kind == "date"]


# ---------------------------------------------------------------- what the tools support
ISO_VALUE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?")


class Support:
    """Every figure the tool results in session.log can back."""

    def __init__(self):
        self.money: dict[Decimal, str] = {}               # dollars -> tool name
        self.percent: dict[Decimal, str] = {}
        self.dates: dict[str, str] = {}

    def _add_money(self, value: Decimal, tool: str):
        self.money.setdefault(abs(value), tool)

    def _add_percent(self, value: Decimal, tool: str):
        self.percent.setdefault(abs(value), tool)

    def _add_date(self, iso: str, tool: str):
        if _iso(*iso.split("-")):
            self.dates.setdefault(iso, tool)

    def _add_display(self, text: str, tool: str):
        """`*_usd` / `*_display` strings: fmt.py made them from numbers in the same result."""
        for _, _, kind, _, value, _ in _scan(text):
            if kind == "money" and value is not None:
                self._add_money(value, tool)
            elif kind == "percent" and value is not None:
                self._add_percent(value, tool)

    def add(self, value, tool: str, key: str = "", depth: int = 0):
        if depth > 40:
            return
        k = key.lower()
        if isinstance(value, dict):
            for sub, v in value.items():
                if str(sub).lower() not in ECHO_KEYS:
                    self.add(v, tool, str(sub), depth + 1)
        elif isinstance(value, (list, tuple, set)):
            for v in value:
                self.add(v, tool, key, depth + 1)
        elif isinstance(value, bool) or value is None:
            return
        elif isinstance(value, (int, float, Decimal)):
            try:
                number = Decimal(str(value))
            except InvalidOperation:
                return
            if not number.is_finite():
                return
            if "cents" in k:
                self._add_money(number / 100, tool)
            else:
                self._add_money(number, tool)
                self._add_percent(number, tool)
                if -1 <= number <= 1:
                    self._add_percent(number * 100, tool)
        elif isinstance(value, (date, datetime)):
            self._add_date(value.isoformat()[:10], tool)
        elif isinstance(value, str):
            text = value.strip()
            if "cents" in k and re.fullmatch(r"-?\d+", text):        # "1800" stored as text
                self._add_money(Decimal(text) / 100, tool)
            elif k.endswith("_usd") or k.endswith("_display"):
                self._add_display(text, tool)
            elif k not in TEXT_KEYS:
                m = ISO_VALUE.fullmatch(text)
                if m:
                    self._add_date(m.group(1), tool)

    @staticmethod
    def _match(table: dict[Decimal, str], value: Decimal | None, gran: Decimal) -> str | None:
        if value is None:
            return None
        if value in table:
            return table[value]
        half = gran / 2
        for cand, tool in table.items():
            if abs(cand - value) <= half:
                return tool
        return None

    def money_source(self, value, gran):
        return self._match(self.money, value, gran)

    def percent_source(self, value, gran):
        return self._match(self.percent, value, gran)

    def date_source(self, key: str) -> str | None:
        if key in self.dates:
            return self.dates[key]
        if key.startswith("--"):
            for d, tool in self.dates.items():
                if d[4:] == key[1:]:
                    return tool
        return None


def _ok_result(entry) -> bool:
    result = entry.get("result")
    return (str(entry.get("name") or "") in TOOL_NAMES and isinstance(result, dict) and "error" not in result)


def support_from(session) -> Support:
    support = Support()
    for entry in getattr(session, "log", None) or []:
        if isinstance(entry, dict) and _ok_result(entry):
            support.add(entry["result"], str(entry["name"]))
    return support


def figures(text: str, support: Support) -> tuple[list[tuple[str, str]], list[str]]:
    """([(figure, tool it came from)], [unsupported figures]) in the order they appear."""
    found, missing = [], []
    for _, _, kind, raw, value, gran in _scan(text):
        if kind == "money":
            tool = support.money_source(value, gran)
        elif kind == "percent":
            tool = support.percent_source(value, gran)
        else:
            tool = support.date_source(value)
        (found.append((raw, tool)) if tool else missing.append(raw))
    return found, missing


def check(text: str, session) -> tuple[bool, list[str]]:
    _, missing = figures(text, support_from(session))
    unique = list(dict.fromkeys(missing))
    return not unique, unique


APPLIED_CLAIM = re.compile(
    r"(?i)\bI(?:'ve|\s+have)?\s+(?:just\s+|already\s+|now\s+)?(?:applied|approved|changed|updated|scheduled|executed|"
    r"raised|lowered|went\s+ahead)\b"
    r"|\b(?:has|have)\s+(?:now\s+|already\s+|just\s+)?been\s+(?:applied|approved|executed|put\s+live)\b"
    r"|\b(?:was|were|is|are|got|gets)\s+(?:now\s+|already\s+|just\s+)?(?:applied|approved|executed)\b"
    r"|\b(?:is|are)\s+(?:now\s+)?live\b|\bnow\s+live\b|\bin\s+effect\b|\bwent\s+ahead\b"
    r"|\bnow\s+(?:costs?|charges?|sells?\s+for|priced\s+at)\b"
    r"|\bwe(?:'ve|\s+have)\s+(?:applied|approved|changed|updated|raised|lowered)\b")


def _has_unsupported(sentence: str, support: Support) -> bool:
    return bool(figures(sentence, support)[1])


def enforce(text: str, session) -> tuple[str, list[str]]:
    """Deterministic last resort: drop every sentence (or line) with an unsupported figure or a claim that a change
    was made, and append "Figures I could verify:" with the supported figures and their tools.
    Returns (text, removed sentences)."""
    text = normalize(text)
    support = support_from(session)
    kept, removed = [], []
    for line in text.split("\n"):
        parts = [p for p in re.split(r"(?<=[.!?])\s+", line) if p.strip()]
        good = [p for p in parts if not _has_unsupported(p, support) and not APPLIED_CLAIM.search(p)]
        removed.extend(p for p in parts if p not in good)
        if good or not parts:
            kept.append(" ".join(good))
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    found, _ = figures(text, support)
    lines = list(dict.fromkeys(f"- {raw} ({tool})" for raw, tool in found))
    tail = "Figures I could verify:\n" + ("\n".join(lines) if lines else "- none of the figures in the draft")
    return ((body + "\n\n") if body else "") + tail, removed


def claims_applied(text: str) -> bool:
    return bool(APPLIED_CLAIM.search(normalize(text)))
