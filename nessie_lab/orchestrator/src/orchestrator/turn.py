"""One owner turn: brief shortcut, card clicks, or the ASI:One tool loop behind the figure guard.

turn(session, user_text, llm=None, ..., owner=True) -> {"text", "suggestions", "degraded", "notes"}

- "How are we doing" / "status" / "summary": answered from brief(session) directly, no LLM.
- A card click, JSON {"action": "save"|"discard", "suggestion": <path>}: never approves anything. "save" only
  confirms the pending suggestion file this session proposed and repeats its approve command; "discard" drops it
  from this chat. Approval happens only at the owner's terminal (`changes approve`, which needs the passphrase).
- Anything else: the tool loop. The answer is normalised first (guard.normalize: NFKC, no zero-width or other
  format characters, one kind of dash), and that exact string is what gets guarded and sent. Every money figure,
  date and percent in it must be in a tool result; if not, the model gets one chance to fix them, then unsupported
  sentences are cut (guard.enforce).
- owner=False (a mailbox chat user who isn't in ORCHESTRATOR_OWNERS): facts only. The model gets no propose_fix
  or preview_suggestion, a call to either is refused, and the brief writes no suggestion ("ask the owner"). The
  brief for such users is cached per (root, day) in `brief_cache`.
- One deadline for the whole turn (Deadline, kept on session.stop): tools.call refuses to start or write once it
  passes, the brief fallback only gets what is left, and the deadline is cancelled when the turn returns, so a
  tool thread left over from a timed-out turn writes nothing more.
- No key, no client, or any loop error: the deterministic brief, degraded=True, with a note saying why.
- Every reply ends with the fixed line FOOTER.
- Before the turn, bk_runner.pre_turn adds a note when the Bookkeeper state is behind the bank (and, only with
  ORCHESTRATOR_BK_AUTO=1 and a delta-capable Bookkeeper, runs a quick delta update within the deadline)."""
import asyncio
import importlib
import inspect
import json
import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import guard
from .llm import SYSTEM_PROMPT, ASIClient, LLMError, run_tool_loop

LOG = logging.getLogger("orchestrator.turn")
MAX_INPUT = 4000
DEADLINE = 60
FOOTER = "Nothing has been changed; approve at your terminal to apply."
OWNER_ONLY = frozenset(("propose_fix", "preview_suggestion"))
PUBLIC_PROMPT = SYSTEM_PROMPT + """
8. This chat is NOT from the restaurant's owner. Share facts only. You cannot propose or preview changes here: if
   they ask for a change, tell them to ask the owner."""
BRIEF_CACHE_MAX = 32
BRIEF_INTENT = re.compile(r"(?i)^\W*(?:(?:so|hey|ok|okay)\W+)?(?:how\s+(?:are|r)\s+we\s+doing|how(?:'s|\s+is)\s+"
                          r"(?:business|it\s+going|the\s+restaurant(?:\s+doing)?)|status|summary|summari[sz]e|brief(?:ing)?|"
                          r"overview|give\s+me\s+(?:a|the)\s+(?:status|summary|brief(?:ing)?|overview))\b[\w\s]{0,30}\W*$")
ESCAPES = guard.ESCAPES
DROP = guard.DROP


class Deadline:
    """A turn's wall-clock budget. tools.call checks .expired() (duck-typed, through session.stop)."""

    def __init__(self, seconds: float):
        self.end = time.monotonic() + max(0.0, float(seconds))
        self.cancelled = False

    def remaining(self) -> float:
        return 0.0 if self.cancelled else max(0.0, self.end - time.monotonic())

    def expired(self) -> bool:
        return self.cancelled or time.monotonic() >= self.end

    def cancel(self):
        self.cancelled = True


def clean_text(value, cap: int = MAX_INPUT) -> str:
    """guard.normalize (terminal escapes removed, NFKC, control/format/surrogate/private/unassigned characters
    dropped with newline and tab kept, dashes unified, digits ASCII), trimmed and cut to `cap`."""
    if value is None:
        return ""
    return guard.normalize(value).strip()[:cap].strip()


def _core(name: str):
    return importlib.import_module(f"orchestrator.{name}")


def proposals(session) -> list[dict]:
    """propose_fix results in session.log that produced a suggestion file: [{"path", "approve_command", ...}]."""
    out, seen = [], set()
    discarded = getattr(session, "discarded", None) or set()
    for entry in getattr(session, "log", None) or []:
        if not isinstance(entry, dict) or entry.get("name") != "propose_fix":
            continue
        result = entry.get("result")
        if not isinstance(result, dict) or result.get("error"):
            continue
        path = result.get("path") or result.get("suggestion_path") or result.get("file")
        if isinstance(path, str) and path and path not in seen and path not in discarded:
            seen.add(path)
            out.append({**result, "path": path})
    return out


def suggestion_paths(session) -> list[str]:
    return [p["path"] for p in proposals(session)]


def _approve_footer(text: str, session) -> str:
    """Make sure every proposed suggestion ends with its terminal command, whatever the model wrote."""
    lines = []
    for p in proposals(session):
        cmd = p.get("approve_command")
        if isinstance(cmd, str) and cmd and cmd not in text:
            lines.append(f"- {cmd}")
    if not lines:
        return text
    return (text + "\n\nNothing has been changed. To approve, run this at your own terminal (it asks for the owner "
            "passphrase):\n" + "\n".join(lines))


def with_footer(text: str) -> str:
    text = (text or "").rstrip()
    return text if text.endswith(FOOTER) else (text + "\n\n" + FOOTER if text else FOOTER)


def card_action(user_text: str) -> dict | None:
    text = (user_text or "").strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict) and data.get("action") in ("save", "discard") and isinstance(data.get("suggestion"), str):
        return {"action": data["action"], "suggestion": data["suggestion"]}
    return None


def _result(text, session, degraded=False, notes=None):
    return {"text": with_footer(text), "suggestions": suggestion_paths(session), "degraded": degraded,
            "notes": list(notes or [])}


def handle_card(session, action: dict) -> dict:
    """Never approves, never applies. Only paths this session's propose_fix returned are recognised."""
    known = {p["path"]: p for p in proposals(session)}
    pending = known.get(action["suggestion"])
    if pending is None:
        return _result("I don't have that suggestion in this chat, so there is nothing to save or discard. "
                       "Nothing was changed.", session, notes=["card: unknown suggestion"])
    if action["action"] == "discard":
        try:
            discarded = getattr(session, "discarded", None)
            if discarded is None:
                discarded = set()
                setattr(session, "discarded", discarded)
            discarded.add(pending["path"])
        except AttributeError:
            pass
        return _result(f"Discarded from this chat: {pending['path']}. Nothing was changed, and it will never be "
                       "applied unless you approve it at your terminal.", session, notes=["card: discard"])
    cmd = pending.get("approve_command") or "(no approve command was returned; ask me to propose it again)"
    return _result(f"Saved for review: {pending['path']}. It is pending, not applied. To approve it, run this at "
                   f"your own terminal (it asks for the owner passphrase):\n{cmd}", session, notes=["card: save"])


def _brief_text(b) -> str:
    if isinstance(b, dict):
        return str(b.get("text") or "")
    return str(b or "")


def _today():
    return datetime.now(ZoneInfo("America/Los_Angeles")).date()


async def from_brief(session, notes, degraded, stop: Deadline | None = None, owner: bool = True,
                     cache: dict | None = None) -> dict:
    """The deterministic brief, inside what is left of the turn's deadline. owner=False: no suggestions, and the
    text is cached per (root, day) in `cache` so a crowd of chat users costs one brief a day."""
    key = None
    if not owner and cache is not None:
        key = (str(getattr(session, "root", "")), _today().isoformat())
        if key in cache:
            return _result(cache[key], session, degraded, [*notes, "brief: today's cached copy"])
    remaining = stop.remaining() if stop is not None else float(DEADLINE)
    if remaining < 1:
        return _result("I ran out of time before I could build the brief. Ask again in a moment.", session, True,
                       [*notes, "deadline reached before the brief"])
    fn = _core("brief").brief
    try:
        work = (lambda: fn(session)) if owner else (lambda: fn(session, propose=False))
        b = await asyncio.wait_for(asyncio.to_thread(work), remaining)
        text = _brief_text(b) or "No brief is available yet."
        if key is not None and _brief_text(b):
            if len(cache) >= BRIEF_CACHE_MAX:
                cache.clear()
            cache[key] = text
    except asyncio.TimeoutError:
        if stop is not None:
            stop.cancel()                                 # the brief's thread may still run: it must write nothing
        text = "The brief took too long, so I stopped it; nothing more was written."
        notes = [*notes, "brief timed out"]
        degraded = True
    except Exception as exc:
        text = "I can't build the brief right now."
        notes = [*notes, f"brief failed ({type(exc).__name__})"]
        degraded = True
    return _result(_approve_footer(text, session), session, degraded, notes)


def _public_dispatch(dispatch):
    """For a non-owner: propose_fix and preview_suggestion are refused before they reach any tool."""
    async def run(session, name, args):
        if name in OWNER_ONLY:
            return {"error": "not available in this chat: only the owner can propose changes; tell them to ask the owner"}
        if dispatch is None:
            from .llm import _default_dispatch
            return await _default_dispatch(session, name, args)
        out = dispatch(session, name, args)
        return await out if inspect.isawaitable(out) else out
    run.notes = getattr(dispatch, "notes", None)
    return run


async def turn(session, user_text: str, llm: ASIClient | None = None, dispatch=None, deadline: float = DEADLINE,
               owner: bool = True, brief_cache: dict | None = None) -> dict:
    stop = Deadline(deadline)
    try:
        session.stop = stop
    except AttributeError:
        pass
    try:
        pre = [] if card_action(clean_text(user_text)) else await _bookkeeper_notes(session, stop, dispatch)
        out = await _turn(session, user_text, llm, dispatch, stop, owner, brief_cache)
        if pre and isinstance(out, dict):
            out["notes"] = [*pre, *(out.get("notes") or [])]
        return out
    finally:
        stop.cancel()                                     # leftover tool threads from this turn write nothing


async def _bookkeeper_notes(session, stop, dispatch) -> list[str]:
    """bk_runner.pre_turn: a note when the Bookkeeper state is behind the bank; with ORCHESTRATOR_BK_AUTO=1 and a
    delta-capable Bookkeeper, a quick delta update inside this turn's deadline. Never a full rerun; never raises."""
    try:
        from .bk_runner import pre_turn
        return await pre_turn(session, stop, dispatch)
    except Exception:
        return []


async def _turn(session, user_text, llm, dispatch, stop: Deadline, owner: bool, brief_cache) -> dict:
    text = clean_text(user_text)
    action = card_action(text)
    if action:
        return handle_card(session, action)
    if not text:
        return _result("Ask me about cash, bills, the menu, findings or the forecast.", session, notes=["empty"])
    if BRIEF_INTENT.match(text):
        return await from_brief(session, ["answered from the deterministic brief"], False, stop, owner, brief_cache)
    if llm is None:
        llm = ASIClient()
    if not getattr(llm, "available", False):
        return await from_brief(session, ["ASI:One unavailable (no API key); showing the deterministic brief"], True,
                                stop, owner, brief_cache)
    notes, counts = [], {}
    kw = {"dispatch": dispatch, "counts": counts}
    if not owner:
        kw.update(dispatch=_public_dispatch(dispatch), system=PUBLIC_PROMPT,
                  tools=[t for t in _core("tools").TOOLS if t["function"]["name"] not in OWNER_ONLY])
    try:
        out = await run_tool_loop(session, text, llm, deadline=stop.remaining(), **kw)
        answer = clean_text(out["text"], 20_000)          # normalised first: this exact string is guarded and sent
        ok, missing = guard.check(answer, session)
        if not ok or guard.claims_applied(answer):
            remaining = stop.remaining()
            if remaining > 5:
                fix = ("Your answer had figures that are in no tool result: " + ", ".join(missing[:20]) +
                       ". Rewrite it using only figures that appear in tool results (call a tool if you need one), "
                       "write dates as YYYY-MM-DD, and do not say any change was applied." if missing else
                       "Rewrite your answer without saying any change was applied: nothing changes until the owner "
                       "approves at the terminal.")
                try:
                    fixed = await run_tool_loop(session, fix, llm, max_steps=3, deadline=remaining,
                                                history=out["messages"], **kw)
                    answer = clean_text(fixed["text"], 20_000)
                    notes.append("guard: asked the model once to fix unsupported figures")
                except LLMError as exc:
                    notes.append(f"guard: fix-up failed ({exc})")
            ok, missing = guard.check(answer, session)
            if not ok or guard.claims_applied(answer):
                answer, removed = guard.enforce(answer, session)
                notes.append(f"guard: removed {len(removed)} sentence(s) with unsupported figures or claims")
    except LLMError as exc:
        return await from_brief(session, [f"ASI:One failed ({exc}); showing the deterministic brief"], True, stop,
                                owner, brief_cache)
    except Exception as exc:
        LOG.warning("turn failed, falling back to the brief", exc_info=True)
        return await from_brief(session, [f"controller error ({type(exc).__name__}); showing the deterministic brief"],
                                True, stop, owner, brief_cache)
    notes.extend(getattr(dispatch, "notes", None) or [])          # agent mode: which peer calls fell back
    return _result(_approve_footer(answer, session), session, bool(getattr(dispatch, "degraded", False)), notes)
