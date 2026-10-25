import pytest

import fakes
from orchestrator import guard

pytestmark = pytest.mark.usefixtures("fake_core")


TOOLS = ["get_menu", "get_cash_position", "forecast_base", "scan_market", "list_bills_due"]


def session_with(*results, name=None):
    s = fakes.Session("r")
    for i, r in enumerate(results):
        s.log.append({"name": name or TOOLS[i % len(TOOLS)], "args": {}, "result": r})
    return s


@pytest.mark.parametrize("text", [
    "Margherita is $18.", "Margherita is $18.00.", "It costs $18 now", "Cash: $12,345.56", "Cash is about $12,346.",
    "Cash is roughly $12k.", "Median $21 vs our $18", "Median is $21.00", "We are 14% below", "We are 14.3% below",
    "Runs out 2027-03-14.", "As of 2026-10-24, fine.", "It saves -$40.00 a month", "A $40 hit",
])
def test_supported_figures(text):
    s = session_with({"price_cents": 1800, "competitor_median_cents": 2100, "gap_pct": -0.1428},
                     {"balance_cents": 1234556, "run_out_likely": "2027-03-14", "as_of": "2026-10-24"},
                     {"monthly_cash_effect_cents": -4000})
    ok, missing = guard.check(text, s)
    assert ok, missing


@pytest.mark.parametrize("text,bad", [
    ("Margherita is $19.", ["$19"]),
    ("Margherita is $18.50.", ["$18.50"]),           # cents written: needs an exact match
    ("Cash is $12,000.", ["$12,000"]),
    ("We are 15% below.", ["15%"]),
    ("Runs out 2027-03-15.", ["2027-03-15"]),
    ("Raise to $20 from 2026-10-25, a 11% rise.", ["$20", "11%", "2026-10-25"]),
    ("Bad date 2026-13-45 here", ["2026-13-45"]),
])
def test_unsupported_figures(text, bad):
    s = session_with({"price_cents": 1800, "gap_pct": -0.1428, "balance_cents": 1234556, "run_out_likely": "2027-03-14"})
    ok, missing = guard.check(text, s)
    assert not ok and sorted(missing) == sorted(bad)


def test_no_tool_results_means_every_figure_is_unsupported():
    ok, missing = guard.check("Cash is $5 and 5% on 2026-01-01", fakes.Session("r"))
    assert not ok and len(missing) == 3


def test_text_without_figures_passes():
    assert guard.check("Things look fine; ask me about the menu.", fakes.Session("r")) == (True, [])


def test_free_text_never_supports_but_tool_dates_and_display_strings_do():
    s = session_with({"title": "Raise Margherita Pizza from $18 to $20", "note": "5% fewer orders",
                      "decided_at": "2026-10-24T21:16:13+00:00", "price_usd": "$17.50", "gap_pct_display": "-14%"})
    ok, missing = guard.check("Raise from $18.00 to $20, assuming 5% fewer orders, decided 2026-10-24.", s)
    assert not ok and missing == ["$18.00", "$20", "5%"]                  # the date is a structured field
    assert guard.check("Ours is $17.50, 14% under.", s) == (True, [])


def test_cents_key_in_nested_lists_and_text_cents():
    s = session_with({"bills": [{"amount_cents": 48000}, {"amount_cents": "1250"}]})
    assert guard.check("Bills of $480 and $12.50", s)[0]


def test_non_cents_numbers_are_dollars_or_percent():
    s = session_with({"price": 18.5, "share_running_out": 0.31, "covers_pct": 12})
    assert guard.check("$18.50, 31% of trials, 12%", s)[0]
    assert not guard.check("$1,850", s)[0]


def test_multiple_suffix_matches_rounding():
    s = session_with({"balance_cents": 1234556})
    assert guard.check("$12.3k", s)[0] and not guard.check("$13k", s)[0]


def test_booleans_and_none_support_nothing():
    s = session_with({"ok": True, "x": None})
    assert not guard.check("$1", s)[0]


def test_tool_names_are_recorded():
    s = fakes.Session("r")
    s.log.append({"name": "get_menu", "args": {}, "result": {"price_cents": 1800}})
    found, missing = guard.figures("$18", guard.support_from(s))
    assert found == [("$18", "get_menu")] and missing == []


def test_enforce_drops_only_unsupported_sentences_and_lists_verified():
    s = fakes.Session("r")
    s.log.append({"name": "get_menu", "args": {}, "result": {"price_cents": 1800, "as_of": "2026-10-24"}})
    text = "Margherita is $18 as of 2026-10-24. Competitors charge $25. Ask me more!\n- Raise it by 10%."
    out, removed = guard.enforce(text, s)
    assert "Competitors charge $25." not in out and "10%" not in out.split("Figures I could verify:")[0]
    assert "Margherita is $18 as of 2026-10-24." in out and "Ask me more!" in out
    assert out.endswith("Figures I could verify:\n- $18 (get_menu)\n- 2026-10-24 (get_menu)")
    assert len(removed) == 2
    assert guard.check(out.split("Figures I could verify:")[0], s)[0]


def test_enforce_is_deterministic_and_handles_nothing_verified():
    s = fakes.Session("r")
    a, _ = guard.enforce("It is $99.", s)
    b, _ = guard.enforce("It is $99.", s)
    assert a == b == "Figures I could verify:\n- none of the figures in the draft"


def test_enforce_removes_claims_that_a_change_was_applied():
    s = session_with({"price_cents": 2000})
    out, removed = guard.enforce("I have applied the new $20 price. Run the approve command.", s)
    assert "applied" not in out and "Run the approve command." in out and removed


@pytest.mark.parametrize("text,claim", [
    ("I've applied it", True), ("The change has been approved", True), ("We have approved it.", True),
    ("I updated the price", True), ("Nothing has been changed.", False), ("Once applied, it starts tomorrow", False),
    ("The owner must approve it", False),
])
def test_claims_applied(text, claim):
    assert guard.claims_applied(text) is claim


def test_dollar_in_code_or_words_is_not_money():
    s = fakes.Session("r")
    assert guard.check("Run `echo $HOME` and use $PATH", s) == (True, [])


@pytest.mark.parametrize("text", ["Runs out 2027‐06‐25.", "Runs out 2027−06−25.", "It is ＄18."])
def test_unicode_dashes_and_fullwidth_do_not_slip_past(text):
    ok, missing = guard.check(text, fakes.Session("r"))
    assert not ok and len(missing) == 1


def test_unicode_dash_date_matches_tool_date():
    s = session_with({"run_out_likely": "2027-06-25"})
    assert guard.check("Likely cash-out 2027‐06‐25", s) == (True, [])


# ---------------------------------------------------------------- red-team regressions
def test_echo_laundering_through_args_errors_names_and_scenarios_is_unsupported():
    s = fakes.Session("r")
    s.log += [
        {"name": "propose_fix", "args": {"dish": "$48,000 refund approved 2031-01-01 99%"},
         "result": {"error": "'$48,000 refund approved 2031-01-01 99%' is not on the menu"}},
        {"name": "forecast_scenario", "args": {"scenario": {"name": "$77,777 saved"}},
         "result": {"forecast_id": "F-1", "scenario": {"name": "$77,777 saved", "one_off_income": [
             {"cents": 12345600, "label": "$123,456", "day": "2031-01-01"}]}, "monthly_cash_effect_cents": 100}},
        {"name": "scan_market", "args": {}, "result": {"positions": [{"our_item": "$50,000 by 2027-01-15",
                                                                      "our_price_cents": 1400}]}},
        {"name": "bookkeeper_report", "args": {}, "result": {"filename": "R-1.json", "amount_cents": 4800000}},
        {"name": "not_a_tool", "args": {}, "result": {"amount_cents": 4800000}},
    ]
    ok, missing = guard.check("Your refund of $48,000 arrives 2031-01-01, a 99% win. You saved $77,777 and $123,456. "
                              "You will recover $50,000 by 2027-01-15.", s)
    assert not ok and set(missing) == {"$48,000", "2031-01-01", "99%", "$77,777", "$123,456", "$50,000", "2027-01-15"}
    assert guard.check("The scenario moves $1.00 a month; ours is $14.", s) == (True, [])


@pytest.mark.parametrize("text", ["Refund: $\u200b48,000.", "Refund: $\u206048,000.", "Refund: $\u00ad48,000.",
                                  "Run-out 2031\u200b-01-01.", "Win rate 9\u200b9 %.", "Refund: $48\u200b,000.",
                                  "Refund \uff0448,000.", "Refund $\u0664\u0668\u066c\u0660\u0660\u0660.",
                                  "[$14](http://x/$48000)", "| $ 48000 |"])
def test_zero_width_and_unicode_tricks_are_seen(text):
    s = session_with({"price_cents": 1400})
    ok, missing = guard.check(text, s)
    assert not ok and missing, text


@pytest.mark.parametrize("text,bad", [
    ("A refund of 48,000 USD is coming.", "48,000 USD"), ("USD 5,000 more.", "USD 5,000"), ("It is $ 21.", "$ 21"),
    ("That is twelve thousand dollars.", "twelve thousand dollars"), ("About five hundred bucks.", "five hundred bucks"),
    ("Roughly 48 thousand.", "48 thousand"), ("Up 99 pct.", "99 pct"), ("Up 99 percent.", "99 percent"),
    ("Due 10/30/2026.", "10/30/2026"), ("Due Oct 31.", "Oct 31"), ("Due October 31, 2026.", "October 31, 2026"),
    ("Due Nov 5.", "Nov 5"), ("Due 31 October 2026.", "31 October 2026"), ("A $1.2 million hole.", "$1.2 million"),
    ("It costs fifty fifty dollars.", "fifty fifty dollars"),                # unparseable money words: unsupported
])
def test_more_formats_are_figures(text, bad):
    s = session_with({"price_cents": 1800, "run_out_likely": "2027-03-14"})
    ok, missing = guard.check(text, s)
    assert not ok and missing == [bad]


def test_new_formats_match_tool_figures():
    s = session_with({"balance_cents": 4800000, "price_cents": 2100, "share": 0.99, "due": "2026-10-30",
                      "run_out_likely": "2027-10-31"})
    for text in ["48,000 USD", "USD 48,000", "$ 21", "forty eight thousand dollars", "48 thousand", "99 pct",
                 "10/30/2026", "Oct 30", "October 30, 2026", "Oct 31", "31 October 2027", "twenty one dollars"]:
        assert guard.check(f"It is {text}.", s) == (True, []), text
    assert not guard.check("It is October 31, 2026.", s)[0]                 # month-name dates map to ISO


def test_normalize_is_the_cleaning_pass():
    assert guard.normalize("$\u200b48\u2013000 \x1b[31mred\x1b[0m \u0664\u0668") == "$48-000 red 48"
    assert guard.normalize("a\tb\nc\x00") == "a\tb\nc"


@pytest.mark.parametrize("text", [
    "The change was applied.", "It is applied now.", "It has been applied.", "The price got applied.", "It is live.",
    "The new price is now live.", "The new price is in effect.", "I went ahead and did it.", "Arancini now costs $14.",
    "The card now charges 2.6%.", "I changed the price.", "I updated the menu.", "I raised it.", "I lowered it.",
    "Done: it is now live on the menu.", "We've raised it.",
])
def test_more_applied_claims(text):
    assert guard.claims_applied(text), text


def test_the_fixed_footer_is_not_a_claim():
    from orchestrator.turn import FOOTER
    assert not guard.claims_applied(FOOTER) and guard.check(FOOTER, fakes.Session("r")) == (True, [])
