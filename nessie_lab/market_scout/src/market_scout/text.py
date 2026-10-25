"""Text from outside (web pages, Google Maps, ASI) made safe to store and to print.

clean(): NFKC, terminal escape sequences removed, then every character in Unicode categories Cc (controls), Cf
(format: zero-width spaces, bidi overrides), Cs (lone surrogates, which SQLite and UTF-8 refuse), Co (private use)
and Cn (unassigned) dropped, newline kept. safe(): the same for one printed line (no newline either)."""
import re
import unicodedata

DROP = frozenset(("Cc", "Cf", "Cs", "Co", "Cn"))
# CSI (ESC [ ... final), OSC (ESC ] ... BEL or ESC \), other two-character escapes, and the 8-bit C1 CSI/OSC forms
ESCAPES = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]|(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?"
                     r"|\x1b[ -/]*[0-~]")


def _strip(text: str, keep_newline: bool) -> str:
    text = ESCAPES.sub("", text)
    try:
        text = unicodedata.normalize("NFKC", text)
    except UnicodeError:                                  # never expected, but a bad string must not raise here
        pass
    return "".join(ch for ch in text if (keep_newline and ch == "\n") or unicodedata.category(ch) not in DROP)


def clean(value, cap: int) -> str | None:
    """Text safe to store: see the module docstring; trimmed and cut to `cap`. Empty -> None."""
    if value is None:
        return None
    text = _strip(str(value), keep_newline=True).strip()[:cap].strip()
    return text or None


def safe(value) -> str:
    """One line safe to print to a terminal."""
    return "" if value is None else _strip(str(value), keep_newline=False)
