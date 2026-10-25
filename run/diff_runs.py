#!/usr/bin/env python3
"""What changed since the last run.

    python3 diff_runs.py --before out_sep --after out_oct --out diff_oct

Reads accounts.csv from two runs and writes:
  accounts_diff.csv  one row per customer in either run, with a status:
      new        an exception now, none before (or not in the earlier run)
      resolved   an exception before, none now (or gone from the later run)
      changed    an exception in both, but the unresolved MRR or the dominant cause differs
      persisted  an exception in both, same cause and amount
      unchanged_supported  supported in both
    plus before and after unresolved MRR and cause, and months_persisted: 2 when persisted,
    1 when the exception is only in the later run, blank otherwise. A longer history needs
    a chain of diffs; this compares two runs.
  summary.md         counts, the movement in unresolved MRR, the largest new and resolved, every cause change.
pandas only. No network.
"""
import argparse, os, sys
import pandas as pd

COLS = ["key", "customer_name", "asof_unresolved_mrr", "dominant_cause"]


def load(path):
    a = pd.read_csv(os.path.join(path, "accounts.csv"), dtype={"key": str})
    a = a[COLS].copy()
    a["exception"] = a["dominant_cause"].fillna("supported") != "supported"
    a["asof_unresolved_mrr"] = pd.to_numeric(a["asof_unresolved_mrr"], errors="coerce").fillna(0.0)
    return a.set_index("key")


def classify(b, a):
    """One row per customer in either run, with a status. b and a are load() results."""
    keys = sorted(set(b.index) | set(a.index))
    rows = []
    for k in keys:
        inb, ina = k in b.index, k in a.index
        eb = bool(b.at[k, "exception"]) if inb else False
        ea = bool(a.at[k, "exception"]) if ina else False
        ub = float(b.at[k, "asof_unresolved_mrr"]) if inb else 0.0
        ua = float(a.at[k, "asof_unresolved_mrr"]) if ina else 0.0
        cb = str(b.at[k, "dominant_cause"]) if inb else ""
        ca = str(a.at[k, "dominant_cause"]) if ina else ""
        if ea and not eb:
            st = "new"
        elif eb and not ea:
            st = "resolved"
        elif ea and eb:
            st = "persisted" if (cb == ca and abs(ub - ua) < 0.005) else "changed"
        else:
            st = "unchanged_supported"
        rows.append(dict(key=k, customer_name=(a.at[k, "customer_name"] if ina else b.at[k, "customer_name"]), status=st,
                         before_unresolved_mrr=round(ub, 2), after_unresolved_mrr=round(ua, 2), before_cause=cb, after_cause=ca,
                         months_persisted=(2 if st == "persisted" else (1 if ea else ""))))
    d = pd.DataFrame(rows)
    order = {"new": 0, "changed": 1, "persisted": 2, "resolved": 3, "unchanged_supported": 4}
    return d.sort_values(["status", "after_unresolved_mrr"], key=lambda s: s.map(order) if s.name == "status" else -s).reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", required=True); ap.add_argument("--after", required=True); ap.add_argument("--out", required=True)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    b, a = load(args.before), load(args.after)
    d = classify(b, a)
    d.to_csv(os.path.join(args.out, "accounts_diff.csv"), index=False)

    counts = d["status"].value_counts().to_dict()
    tb, ta = float(b["asof_unresolved_mrr"].sum()), float(a["asof_unresolved_mrr"].sum())
    L = ["# What changed between the two runs", "",
         f"Before: `{args.before}`. After: `{args.after}`.", "",
         "| status | accounts |", "|---|---:|"]
    for s in ["new", "changed", "persisted", "resolved", "unchanged_supported"]:
        L.append(f"| {s} | {counts.get(s, 0)} |")
    L += ["", f"Unresolved MRR: {tb:,.0f} before, {ta:,.0f} after, movement {ta - tb:+,.0f} (ARR {12 * (ta - tb):+,.0f}).", ""]
    for title, st, col in (("Largest new exceptions", "new", "after_unresolved_mrr"), ("Largest resolved", "resolved", "before_unresolved_mrr")):
        sub = d[d["status"] == st].sort_values(col, ascending=False).head(5)
        L += [f"## {title}", ""]
        if not len(sub):
            L.append("None.")
        else:
            L += ["| account | cause | unresolved MRR |", "|---|---|---:|"]
            for _, r in sub.iterrows():
                L.append(f"| {r['customer_name']} | {r['after_cause'] if st == 'new' else r['before_cause']} | {r[col]:,.0f} |")
        L.append("")
    ch = d[d["status"] == "changed"]
    L += ["## Changed", ""]
    if not len(ch):
        L.append("None.")
    else:
        L += ["| account | before | after | unresolved MRR before | after |", "|---|---|---|---:|---:|"]
        for _, r in ch.iterrows():
            L.append(f"| {r['customer_name']} | {r['before_cause']} | {r['after_cause']} | {r['before_unresolved_mrr']:,.0f} | {r['after_unresolved_mrr']:,.0f} |")
    L.append("")
    open(os.path.join(args.out, "summary.md"), "w").write("\n".join(L))
    print("diff:", ", ".join(f"{s} {counts.get(s, 0)}" for s in ["new", "changed", "persisted", "resolved", "unchanged_supported"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
