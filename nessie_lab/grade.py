"""Score a flags file against the answer key (grader-only).

  python grade.py data/flags_baseline.json

A flag is a dict with "bank_id" and/or "books_ref". It catches an item if it names one of the item's bank_ids
or its books_ref. Flags on decoy rows count as decoy hits; other flags that catch nothing are false alarms.
"""
import json, sys
from pathlib import Path

key = json.loads((Path(__file__).parent / "data" / "grader" / "answer_key.json").read_text())["items"]
flags = json.loads(Path(sys.argv[1]).read_text())


def refs(item):
    r = item.get("books_ref")
    return set(r if isinstance(r, list) else [r] if r else [])


def hits(flag, item):
    return flag.get("bank_id") in set(item.get("bank_ids", [])) or (flag.get("books_ref") in refs(item) and flag.get("books_ref"))


errors, decoys = [k for k in key if k["should_flag"]], [k for k in key if not k["should_flag"]]
caught = {k["id"]: any(hits(f, k) for f in flags) for k in errors}
decoy_hits = {k["id"]: sum(hits(f, k) for f in flags) for k in decoys}
false_alarms = [f for f in flags if not any(hits(f, k) for k in key)]
true_flags = len(flags) - len(false_alarms) - sum(decoy_hits.values())
print(f"recall {sum(caught.values())}/{len(errors)}  " + " ".join(f"{k}:{'Y' if v else '-'}" for k, v in caught.items()))
print(f"flags {len(flags)} | on errors {true_flags} | decoy hits {sum(decoy_hits.values())} {decoy_hits} | false alarms {len(false_alarms)}")
print(f"precision {true_flags / len(flags):.2f}" if flags else "no flags")
for f in false_alarms[:8]:
    print("  false alarm:", {k: v for k, v in f.items() if v not in (None, 0)})
