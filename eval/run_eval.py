"""Measure the exact checks against the hand-labelled evaluation set.

Usage:  python -m eval.run_eval            (prints a table and writes eval/results.md)

A finding counts as correct when its rule ID matches a labelled issue and the quoted words overlap
(case-insensitive). Claim verification is scored separately: a labelled fact ID must appear among the
verified claims. No AI is involved, so the numbers are exactly repeatable.
"""
import json
from collections import defaultdict
from datetime import date
from pathlib import Path

from app import engine

HERE = Path(__file__).parent


def load_kit():
    data = json.loads((HERE.parent / "app" / "seed" / "codecraft.json").read_text(encoding="utf-8"))
    data.pop("samples", None)
    data.pop("history", None)
    data["version"] = 1
    return data


def _match(found, label):
    rid, quote = label
    if found["rule_id"] != rid:
        return False
    a, b = found["quote"].lower(), quote.lower()
    return a == b or (a and b and (a in b or b in a))


def evaluate():
    spec = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
    kit, today = load_kit(), spec["today"]
    per = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    misses, extras = [], []
    v_total = v_hit = 0
    for case in spec["cases"]:
        intent = {"cta": case["cta"]} if case.get("cta") else None
        findings, verified = engine.run_rule_checks(case["text"], kit, intent, today=today)
        labels = [tuple(x) for x in case["expect"]]
        used = set()
        for f in findings:
            hit = next((i for i, lab in enumerate(labels) if i not in used and _match(f, lab)), None)
            family = f["rule_id"] if not f["rule_id"].startswith("TERM") else "TERM (glossary)"
            if hit is None:
                per[family]["fp"] += 1
                extras.append((case["id"], f["rule_id"], f["quote"]))
            else:
                used.add(hit)
                per[family]["tp"] += 1
        for i, (rid, quote) in enumerate(labels):
            if i not in used:
                per[rid if not rid.startswith("TERM") else "TERM (glossary)"]["fn"] += 1
                misses.append((case["id"], rid, quote))
        got = {v["fact_id"] for v in verified}
        for fid in case.get("verified", []):
            v_total += 1
            v_hit += fid in got
    rows = []
    tot = {"tp": 0, "fp": 0, "fn": 0}
    for family in sorted(per):
        c = per[family]
        for k in tot:
            tot[k] += c[k]
        rows.append((family, c["tp"], c["fp"], c["fn"]))
    return {"cases": len(spec["cases"]), "rows": rows, "total": tot, "misses": misses, "extras": extras,
            "verified": (v_hit, v_total)}


def _p(tp, fp):
    return tp / (tp + fp) if tp + fp else 1.0


def _r(tp, fn):
    return tp / (tp + fn) if tp + fn else 1.0


def report(res):
    t = res["total"]
    lines = [f"# Evaluation of the exact checks ({res['cases']} hand-labelled cases)", "",
             "| Check | Correct | False alarms | Missed | Precision | Recall |", "|---|---|---|---|---|---|"]
    for family, tp, fp, fn in res["rows"]:
        lines.append(f"| {family} | {tp} | {fp} | {fn} | {_p(tp, fp):.0%} | {_r(tp, fn):.0%} |")
    lines.append(f"| **All checks** | **{t['tp']}** | **{t['fp']}** | **{t['fn']}** | **{_p(t['tp'], t['fp']):.0%}** | **{_r(t['tp'], t['fn']):.0%}** |")
    vh, vt = res["verified"]
    lines += ["", f"Claim verification: {vh} of {vt} approved claims were verified against the right fact ({vh / vt:.0%}).", ""]
    if res["extras"]:
        lines += ["False alarms:", ""] + [f"- `{c}` {rid}: “{q}”" for c, rid, q in res["extras"]] + [""]
    if res["misses"]:
        lines += ["Missed:", ""] + [f"- `{c}` {rid}: “{q}”" for c, rid, q in res["misses"]] + [""]
    return "\n".join(lines)


if __name__ == "__main__":
    out = report(evaluate())
    (HERE / "results.md").write_text(out + "\n", encoding="utf-8")
    print(out)
