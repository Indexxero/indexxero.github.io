#!/usr/bin/env python3
"""Indexxero pilot kit. Runs on one machine, reads two CSV exports, writes a folder of findings.

    python3 pilot.py --schedule schedule.csv --invoices invoices.csv --map mapping.json --asof 2025-10 --out out/

Steps (each writes a file so the work can be checked by hand):
  1 manifest    sha256, rows, columns, date range of each input          -> manifest.json
  2 profile     blanks, duplicates, unparsed dates and amounts            -> profile.md
  3 match       customers across the two files, method per row           -> match_table.csv
  4 rebuild     invoice lines spread over service months, credits netted -> invoice_mrr_monthly.csv, invoice_lines_classified.csv
  5 compare     schedule MRR beside invoice supported MRR, per month      -> compare_customer_month.csv
  6 exceptions  one bucket per customer month, one row per customer      -> exceptions.csv, accounts.csv
  7 retention   GRR, NRR and cohorts from the schedule and from invoices -> retention.csv, cohorts.csv
  8 report      totals, exceptions by cause, accounts, limitations       -> report.md, pilot.xlsx
  9 supported   the supported schedule in the Indexxero export shape     -> supported_export.csv

Nothing is sent anywhere. No network calls. Pandas and openpyxl only.
"""
import argparse, difflib, hashlib, json, os, re, sys
from datetime import datetime
import pandas as pd

KIT_VERSION = "2026-10-22"


# ---------- helpers ----------
def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def to_amount(s):
    if pd.isna(s):
        return float("nan")
    t = str(s).strip()
    neg = t.startswith("(") and t.endswith(")")
    t = re.sub(r"[^\d.\-]", "", t)
    if t in ("", "-", "."):
        return float("nan")
    v = float(t)
    return -abs(v) if neg else v


def to_date(s):
    if pd.isna(s) or str(s).strip() == "":
        return pd.NaT
    return pd.to_datetime(str(s).strip(), errors="coerce")


def parse_dates(series):
    """Whole column at once: one format guess per column, then a mixed pass for the stragglers."""
    s = series.astype(str).str.strip()
    s = s.where(s != "", None)
    out = pd.to_datetime(s, errors="coerce")
    missing = out.isna() & s.notna()
    if missing.any():
        out[missing] = pd.to_datetime(s[missing], errors="coerce", format="mixed")
    return out


def parse_amounts(series):
    s = series.astype(str).str.strip()
    neg = s.str.startswith("(") & s.str.endswith(")")
    s = s.str.replace(r"[^\d.\-]", "", regex=True)
    s = s.where(~s.isin(["", "-", "."]), None)
    v = pd.to_numeric(s, errors="coerce")
    return v.where(~neg, -v.abs())


def norm_name(s):
    if pd.isna(s):
        return ""
    t = str(s).lower()
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    stop = {"inc", "incorporated", "corp", "corporation", "co", "company", "llc", "ltd", "limited", "plc", "the", "group", "holdings", "gmbh", "sa", "ag"}
    words = [w for w in t.split() if w not in stop]
    return " ".join(words)


def month_key(d):
    return d.strftime("%Y-%m") if not pd.isna(d) else ""


def month_range(a, b):
    return [p.strftime("%Y-%m") for p in pd.period_range(a, b, freq="M")]


def read_table(path):
    if path.lower().endswith((".xlsx", ".xlsm", ".xls")):
        return pd.read_excel(path, dtype=str).fillna("")
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def load_mapped(path, section, mapping):
    raw = read_table(path)
    missing = []
    dups = int(raw.duplicated().sum())
    if dups and section == "schedule":
        missing.append(f"{section}: {dups} exact duplicate rows dropped before any calculation (an export artefact; the dropped rows are listed in profile.md)")
        raw = raw.drop_duplicates().reset_index(drop=True)
    elif dups:
        missing.append(f"{section}: {dups} exact duplicate rows kept; a repeated invoice is a finding, not an artefact, and shows as duplicate_invoice")
    if mapping.get("format") == "wide" and section == "schedule":
        raw, note = melt_wide(raw, mapping)
        missing.append(note)
    out = pd.DataFrame(index=raw.index)
    cur_col = mapping.get("currency")
    if cur_col and cur_col in raw.columns:
        curs = sorted(set(raw[cur_col].astype(str).str.strip().str.upper()) - {""})
        if len(curs) > 1:
            missing.append(f"{section}: more than one currency present ({', '.join(curs)}); amounts are compared as given, no conversion")
    for kit_col, src in mapping.items():
        if kit_col.startswith("_") or kit_col in ("format", "month_columns"):
            continue
        if src and src in raw.columns:
            out[kit_col] = raw[src]
        else:
            out[kit_col] = ""
            if src:
                missing.append(f"{section}.{kit_col} -> '{src}' not found")
    return raw, out, missing


def melt_wide(raw, mapping):
    """One column per month (Jan-25, 2025-01, Jan 2025 ...) to one row per customer per month."""
    name_col = mapping.get("customer_name") or raw.columns[0]
    id_col = mapping.get("customer_id") or ""
    month_cols = []
    for c in raw.columns:
        if c in (name_col, id_col):
            continue
        d = pd.NaT
        for fmt in ("%b-%y", "%b %y", "%b-%Y", "%b %Y", "%Y-%m", "%m/%Y", "%Y%m", "%B %Y", "%B-%y", "%m-%Y", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                d = pd.to_datetime(str(c).strip(), format=fmt); break
            except Exception:
                d = pd.NaT
        if not pd.isna(d) and not (2000 <= d.year <= 2100):
            d = pd.NaT
        if not pd.isna(d):
            month_cols.append((c, pd.Period(d, freq="M").to_timestamp(how="end").strftime("%Y-%m-%d")))
    keep = [name_col] + ([id_col] if id_col else [])
    long = raw.melt(id_vars=keep, value_vars=[c for c, _ in month_cols], var_name="_col", value_name="mrr")
    colmap = dict(month_cols)
    long["snapshot_date"] = long["_col"].map(colmap)
    long = long.drop(columns=["_col"]).rename(columns={name_col: "customer_name"})
    if id_col:
        long = long.rename(columns={id_col: "customer_id"})
    else:
        long["customer_id"] = ""
    long = long[long["mrr"].astype(str).str.strip() != ""]
    for c in ("start_date", "end_date", "status"):
        long[c] = ""
    mapping.update({"customer_name": "customer_name", "customer_id": "customer_id", "snapshot_date": "snapshot_date", "mrr": "mrr", "format": "snapshot"})
    return long.reset_index(drop=True), f"schedule: wide layout, {len(month_cols)} month columns recognised ({month_cols[0][0]} to {month_cols[-1][0]})" if month_cols else "schedule: wide layout but no month columns recognised"


# ---------- 1 manifest ----------
def step_manifest(args, sched_raw, inv_raw, missing):
    m = {
        "kit_version": KIT_VERSION,
        "generated_utc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "asof_month": args.asof,
        "inputs": {
            "schedule": {"file": os.path.basename(args.schedule), "sha256": sha256(args.schedule), "rows": int(len(sched_raw)), "columns": list(sched_raw.columns)},
            "invoices": {"file": os.path.basename(args.invoices), "sha256": sha256(args.invoices), "rows": int(len(inv_raw)), "columns": list(inv_raw.columns)},
            "mapping": {"file": os.path.basename(args.map), "sha256": sha256(args.map)},
        },
        "mapping_warnings": missing,
    }
    return m


# ---------- 2 profile ----------
def step_profile(sched, inv, man):
    lines = ["# Profile of the two inputs", "", f"Generated {man['generated_utc']} UTC. Kit {KIT_VERSION}.", ""]

    def section(name, df, date_cols, amt_cols, id_cols):
        lines.append(f"## {name}: {len(df)} rows")
        lines.append("")
        lines.append("| column | blank | distinct | unparsed |")
        lines.append("|---|---:|---:|---:|")
        for c in df.columns:
            blank = int((df[c].astype(str).str.strip() == "").sum())
            distinct = int(df[c].nunique())
            unparsed = ""
            if c in date_cols:
                nonblank = df[c].astype(str).str.strip() != ""
                unparsed = int((parse_dates(df[c]).isna() & nonblank).sum())
            if c in amt_cols:
                nonblank = df[c].astype(str).str.strip() != ""
                unparsed = int((parse_amounts(df[c]).isna() & nonblank).sum())
            lines.append(f"| {c} | {blank} | {distinct} | {unparsed} |")
        for c in id_cols:
            if c in df.columns:
                dup = int(df[c][df[c].astype(str).str.strip() != ""].duplicated().sum())
                lines.append(f"\nDuplicate values in {c}: {dup}")
        lines.append("")

    section("Schedule", sched, ["snapshot_date", "start_date", "end_date"], ["mrr", "arr"], [])
    section("Invoices", inv, ["invoice_date", "service_start", "service_end"], ["amount"], ["invoice_id"])
    return "\n".join(lines)


# ---------- schedule to monthly ----------
def schedule_monthly(sched, fmt, asof):
    df = sched.copy()
    df["mrr_v"] = parse_amounts(df["mrr"])
    if "arr" in df.columns:
        arr_v = parse_amounts(df["arr"])
        df.loc[df["mrr_v"].isna() & arr_v.notna(), "mrr_v"] = arr_v / 12.0
    df["start_v"] = parse_dates(df["start_date"])
    df["end_v"] = parse_dates(df["end_date"])
    df["cid"] = df["customer_id"].astype(str).str.strip()
    df["cname"] = df["customer_name"].astype(str).str.strip()
    df["key"] = df["cid"].where(df["cid"] != "", "NAME:" + df["cname"].map(norm_name))
    if fmt == "snapshot":
        df["month"] = parse_dates(df["snapshot_date"]).dt.strftime("%Y-%m").fillna("")
        df = df[df["month"] != ""]
        g = df.groupby(["key", "month"], as_index=False).agg(cid=("cid", "first"), cname=("cname", "first"), start=("start_v", "min"), end=("end_v", "max"), status=("status", "last"), sched_mrr=("mrr_v", "sum"))
    else:  # contract
        rows = []
        asof_p = pd.Period(asof, freq="M")
        for _, r in df.iterrows():
            if pd.isna(r["start_v"]):
                continue
            last = r["end_v"] if not pd.isna(r["end_v"]) else asof_p.to_timestamp(how="end")
            for mo in month_range(r["start_v"], last):
                if pd.Period(mo, freq="M") <= asof_p:
                    rows.append(dict(key=r["key"], month=mo, cid=r["cid"], cname=r["cname"], start=r["start_v"], end=r["end_v"], status=r["status"], sched_mrr=r["mrr_v"]))
        g = pd.DataFrame(rows)
        g = g.groupby(["key", "month"], as_index=False).agg(cid=("cid", "first"), cname=("cname", "first"), start=("start", "min"), end=("end", "max"), status=("status", "last"), sched_mrr=("sched_mrr", "sum"))
    g["sched_mrr"] = g["sched_mrr"].fillna(0.0)
    return g


# ---------- 4 rebuild invoices ----------
def classify_line(r, rules):
    t = str(r["line_type"]).strip().lower()
    amt = r["amount_v"]
    if t in rules["credit_types"] or (t == "" and not pd.isna(amt) and amt < 0):
        return "credit"
    if t in rules["non_recurring_types"]:
        return "non_recurring"
    if t in rules["recurring_types"]:
        return "recurring"
    if t == "" and not pd.isna(amt) and amt > 0 and not pd.isna(r["ss_v"]) and not pd.isna(r["se_v"]):
        return "recurring"
    return "unknown_type"


def rebuild(inv, rules):
    df = inv.copy()
    df["amount_v"] = parse_amounts(df["amount"])
    df["inv_date_v"] = parse_dates(df["invoice_date"])
    df["ss_v"] = parse_dates(df["service_start"])
    df["se_v"] = parse_dates(df["service_end"])
    df["cid"] = df["customer_id"].astype(str).str.strip()
    df["cname"] = df["customer_name"].astype(str).str.strip()
    df["key"] = df["cid"].where(df["cid"] != "", "NAME:" + df["cname"].map(norm_name))
    df["kind"] = df.apply(lambda r: classify_line(r, rules), axis=1)
    iid = df["invoice_id"].astype(str).str.strip()
    df["duplicate_id"] = iid.ne("") & iid.duplicated(keep=False)
    df["no_period"] = df["ss_v"].isna() | df["se_v"].isna()
    # a credit column inside the invoice row (Chargebee "Credits Applied") becomes its own credit line
    if "credit_amount" in df.columns:
        cred = parse_amounts(df["credit_amount"]).fillna(0.0)
        extra = df[cred.abs() > 0].copy()
        if len(extra):
            extra["amount_v"] = -cred[cred.abs() > 0].abs().values
            extra["kind"] = "credit"
            extra["invoice_id"] = extra["invoice_id"].astype(str) + ":credit"
            df = pd.concat([df, extra], ignore_index=True)
    # spread each recurring or credit line over its service months
    work = df[df["kind"].isin(["recurring", "credit"]) & df["amount_v"].notna()].copy()
    rows = []
    # fast path: lines whose service period is exactly one whole month, or no period at all
    ss, se = work["ss_v"], work["se_v"]
    one_whole = (~work["no_period"]) & (ss.dt.day == 1) & (se.dt.normalize() == (ss + pd.offsets.MonthEnd(0)).dt.normalize()) & (ss.dt.to_period("M") == se.dt.to_period("M"))
    fast = work[one_whole]
    rows_df = pd.DataFrame({"key": fast["key"], "month": fast["ss_v"].dt.strftime("%Y-%m"), "kind": fast["kind"], "amount": fast["amount_v"], "no_period": False, "duplicate_id": fast["duplicate_id"].astype(bool)})
    nop = work[work["no_period"] & work["inv_date_v"].notna()]
    nop_df = pd.DataFrame({"key": nop["key"], "month": nop["inv_date_v"].dt.strftime("%Y-%m"), "kind": nop["kind"], "amount": nop["amount_v"], "no_period": True, "duplicate_id": nop["duplicate_id"].astype(bool)})
    slow = work[~one_whole & ~work["no_period"]]
    for _, r in slow.iterrows():
        s, e = r["ss_v"], r["se_v"]
        if e < s:
            s, e = e, s
        months = month_range(s, e)
        whole = s.day == 1 and e.normalize() == pd.Period(month_key(e), freq="M").to_timestamp(how="end").normalize()
        total_days = (e - s).days + 1
        for mo in months:
            p = pd.Period(mo, freq="M")
            if whole:
                share = 1.0 / len(months)  # whole months: equal split, so an annual prepay is the same every month
            else:
                ms, me = p.to_timestamp(how="start"), p.to_timestamp(how="end").normalize()
                ov = (min(e, me) - max(s, ms)).days + 1
                if ov <= 0:
                    continue
                share = ov / total_days
            rows.append(dict(key=r["key"], month=mo, kind=r["kind"], amount=r["amount_v"] * share, no_period=False, duplicate_id=bool(r["duplicate_id"])))
    rows = [rows_df, nop_df] + ([pd.DataFrame(rows)] if rows else [])
    spread = pd.concat([x for x in rows if len(x)], ignore_index=True) if any(len(x) for x in rows) else pd.DataFrame(columns=["key", "month", "kind", "amount", "no_period", "duplicate_id"])
    spread["rec_amt"] = spread["amount"].where(spread["kind"] == "recurring", 0.0)
    spread["cred_amt"] = spread["amount"].where(spread["kind"] == "credit", 0.0)
    spread["is_credit"] = spread["kind"] == "credit"
    monthly = spread.groupby(["key", "month"], as_index=False).agg(inv_recurring=("rec_amt", "sum"), inv_credit=("cred_amt", "sum"), has_credit=("is_credit", "any"), has_duplicate_id=("duplicate_id", "any"), has_no_period=("no_period", "any"))
    monthly["inv_mrr"] = monthly["inv_recurring"] + monthly["inv_credit"]
    # non recurring and unknown by customer month (invoice date month)
    nr = df[df["kind"].isin(["non_recurring", "unknown_type"])].copy()
    nr["month"] = nr["inv_date_v"].dt.strftime("%Y-%m").fillna("")
    nr_m = nr.groupby(["key", "month"], as_index=False).agg(non_recurring_amount=("amount_v", "sum"), unknown_lines=("kind", lambda s: int((s == "unknown_type").sum())))
    monthly = monthly.merge(nr_m, on=["key", "month"], how="outer").fillna({"inv_recurring": 0.0, "inv_credit": 0.0, "inv_mrr": 0.0, "non_recurring_amount": 0.0, "unknown_lines": 0, "has_credit": False, "has_duplicate_id": False, "has_no_period": False})
    names = df.groupby("key").agg(cid=("cid", "first"), cname=("cname", "first")).reset_index()
    return df, monthly, names


# ---------- 3 match ----------
def match_customers(sched_names, inv_names):
    s = sched_names.copy(); i = inv_names.copy()
    s["nn"] = s["cname"].apply(norm_name); i["nn"] = i["cname"].apply(norm_name)
    rows = []
    used_inv = set()
    inv_by_id = {r["key"]: r for _, r in i.iterrows() if not str(r["key"]).startswith("NAME:")}
    inv_by_nn = {}
    for _, r in i.iterrows():
        inv_by_nn.setdefault(r["nn"], []).append(r["key"])
    for _, r in s.iterrows():
        k = r["key"]
        if not k.startswith("NAME:") and k in inv_by_id:
            rows.append(dict(sched_key=k, inv_key=k, method="exact_id", confidence="high", sched_name=r["cname"], inv_name=inv_by_id[k]["cname"])); used_inv.add(k); continue
        cands = [c for c in inv_by_nn.get(r["nn"], []) if c not in used_inv] if r["nn"] else []
        if len(cands) == 1:
            rows.append(dict(sched_key=k, inv_key=cands[0], method="normalised_name", confidence="medium", sched_name=r["cname"], inv_name=i.loc[i["key"] == cands[0], "cname"].iloc[0])); used_inv.add(cands[0]); continue
        if len(cands) > 1:
            rows.append(dict(sched_key=k, inv_key="", method="ambiguous_name", confidence="review", sched_name=r["cname"], inv_name="; ".join(cands))); continue
        # fuzzy: one unused invoice name within a small edit distance (Harbour vs Harbor, a dropped word)
        best, score = None, 0.0
        if r["nn"]:
            for nn, keys in inv_by_nn.items():
                free = [c for c in keys if c not in used_inv]
                if not free or not nn:
                    continue
                sc = difflib.SequenceMatcher(None, r["nn"], nn).ratio()
                if sc > score:
                    best, score = free[0], sc
        if best is not None and score >= 0.88:
            rows.append(dict(sched_key=k, inv_key=best, method="fuzzy_name", confidence="review", sched_name=r["cname"], inv_name=i.loc[i["key"] == best, "cname"].iloc[0], score=round(score, 3))); used_inv.add(best); continue
        rows.append(dict(sched_key=k, inv_key="", method="unmatched_schedule_only", confidence="review", sched_name=r["cname"], inv_name=""))
    for _, r in i.iterrows():
        if r["key"] not in used_inv:
            rows.append(dict(sched_key="", inv_key=r["key"], method="unmatched_invoice_only", confidence="review", sched_name="", inv_name=r["cname"]))
    return pd.DataFrame(rows)


# ---------- 5 and 6 compare and exceptions ----------
def compare(sched_m, inv_m, match, rules, asof, ramp_months, inv_names=None):
    link = match[(match["inv_key"] != "") & (match["sched_key"] != "")][["sched_key", "inv_key"]]
    inv_only_keys = set(match.loc[match["method"] == "unmatched_invoice_only", "inv_key"])
    # schedule side, with the matched invoice key
    s = sched_m.merge(link, left_on="key", right_on="sched_key", how="left").drop(columns=["sched_key"])
    s["inv_key"] = s["inv_key"].fillna("")
    # invoice side for matched customers, keyed back to the schedule key, so months billed after churn are kept
    im = inv_m.merge(link, left_on="key", right_on="inv_key", how="inner").drop(columns=["key"])
    cmp = s.merge(im, left_on=["key", "month"], right_on=["sched_key", "month"], how="outer", suffixes=("", "_inv"))
    # rows that exist only on the invoice side: fill the schedule columns
    only_inv = cmp["key"].isna()
    cmp.loc[only_inv, "key"] = cmp.loc[only_inv, "sched_key"]
    cmp.loc[only_inv, "inv_key"] = cmp.loc[only_inv, "inv_key_inv"]
    cmp["sched_mrr"] = cmp["sched_mrr"].fillna(0.0)
    names = sched_m.groupby("key").agg(cid=("cid", "first"), cname=("cname", "first"), status=("status", "last"))
    for c in ["cid", "cname"]:
        cmp[c] = cmp[c].fillna(cmp["key"].map(names[c]))
    cmp["status"] = cmp["status"].fillna("")
    cmp = cmp.drop(columns=[c for c in ["sched_key", "inv_key_inv"] if c in cmp.columns])
    # invoice only customers
    io = inv_m[inv_m["key"].isin(inv_only_keys)].copy()
    io["inv_key"] = io["key"]; io["sched_mrr"] = 0.0; io["cid"] = ""; io["start"] = pd.NaT; io["end"] = pd.NaT; io["status"] = ""
    io["cname"] = io["key"].map(inv_names.set_index("key")["cname"]) if inv_names is not None else ""
    cmp = pd.concat([cmp, io], ignore_index=True, sort=False)
    for c in ["inv_recurring", "inv_credit", "inv_mrr", "non_recurring_amount", "unknown_lines"]:
        cmp[c] = cmp[c].fillna(0.0)
    for c in ["has_credit", "has_duplicate_id", "has_no_period"]:
        cmp[c] = cmp[c].map(lambda v: bool(v) if not pd.isna(v) else False)
    cmp["diff"] = cmp["sched_mrr"] - cmp["inv_mrr"]
    cmp["supported_mrr"] = cmp[["sched_mrr", "inv_mrr"]].min(axis=1).clip(lower=0)
    tol_pct, tol_abs = rules["tolerance_pct"] / 100.0, rules["tolerance_abs"]
    cmp = cmp.sort_values(["key", "month"]).reset_index(drop=True)
    # first month per key for ramp
    first_month = cmp[cmp["sched_mrr"] > 0].groupby("key")["month"].min().to_dict()
    # rolling diff for timing: diff this month + next month nets to ~0
    nxt = cmp.set_index(["key", "month"])["diff"].to_dict()

    def bucket(r):
        a = abs(r["diff"])
        within = a <= max(tol_abs, tol_pct * max(abs(r["sched_mrr"]), abs(r["inv_mrr"])))
        if r["inv_key"] == "" and r["sched_mrr"] > 0:
            return "unmatched_schedule_only"
        if r["key"] in inv_only_keys:
            return "unmatched_invoice_only"
        if r["has_duplicate_id"]:
            return "duplicate_invoice"
        if r["has_credit"]:
            return "credit_or_rebill"
        if r["unknown_lines"] > 0 or (r["non_recurring_amount"] != 0 and r["diff"] < 0 and not within):
            return "classification"
        if within:
            return "supported" if r["sched_mrr"] > 0 or r["inv_mrr"] > 0 else "no_activity"
        p = pd.Period(r["month"], freq="M")
        d_next = nxt.get((r["key"], (p + 1).strftime("%Y-%m")), 0.0)
        d_prev = nxt.get((r["key"], (p - 1).strftime("%Y-%m")), 0.0)
        if abs(r["diff"] + d_next) <= tol_abs * 2 or abs(r["diff"] + d_prev) <= tol_abs * 2:
            return "timing"
        fm = first_month.get(r["key"])
        if fm and r["diff"] > 0 and (p - pd.Period(fm, freq="M")).n < ramp_months:
            return "ramp_or_proration"
        if r["has_no_period"]:
            return "timing"
        return "over_stated" if r["diff"] > 0 else "under_stated"

    cmp["bucket"] = cmp.apply(bucket, axis=1)
    cmp["supported_mrr"] = cmp.apply(lambda r: r["sched_mrr"] if r["bucket"] in ("supported", "timing") else r["supported_mrr"], axis=1)
    return cmp


def accounts_view(cmp, asof, months_back):
    asof_p = pd.Period(asof, freq="M")
    win = [(asof_p - k).strftime("%Y-%m") for k in range(months_back)]
    w = cmp[cmp["month"].isin(win)]
    rows = []
    for key, g in w.groupby("key"):
        bad = g[~g["bucket"].isin(["supported", "no_activity"])]
        cur = g[g["month"] == asof]
        dom = bad["bucket"].value_counts().idxmax() if len(bad) else "supported"
        rows.append(dict(key=key, customer_id=g["cid"].iloc[0], customer_name=str(g["cname"].iloc[0] or ""),
                         asof_schedule_mrr=float(cur["sched_mrr"].sum()), asof_invoice_mrr=float(cur["inv_mrr"].sum()), asof_supported_mrr=float(cur["supported_mrr"].sum()),
                         asof_unresolved_mrr=float(cur["sched_mrr"].sum() - cur["supported_mrr"].sum()), months_in_window=int(len(g)), months_not_supported=int(len(bad)),
                         dominant_cause=dom, causes=", ".join(sorted(bad["bucket"].unique())), net_diff_window=float(g["diff"].sum())))
    acc = pd.DataFrame(rows).sort_values(["asof_unresolved_mrr", "months_not_supported"], ascending=[False, False])
    return acc


# ---------- 7 retention ----------
def retention(cmp, asof, months_back):
    """GRR and NRR over the window ending at asof, on three bases: schedule, invoice, supported."""
    asof_p = pd.Period(asof, freq="M")
    start_m = (asof_p - months_back).strftime("%Y-%m")
    out = []
    for basis, col in [("schedule", "sched_mrr"), ("invoices", "inv_mrr"), ("supported", "supported_mrr")]:
        s0 = cmp[cmp["month"] == start_m].set_index("key")[col].clip(lower=0)
        s0 = s0[s0 > 0]
        s1 = cmp[cmp["month"] == asof].set_index("key")[col].clip(lower=0).reindex(s0.index).fillna(0.0)
        base = float(s0.sum())
        grr = float(pd.concat([s0, s1], axis=1).min(axis=1).sum()) / base if base else float("nan")
        nrr = float(s1.sum()) / base if base else float("nan")
        churned = int((s1 == 0).sum())
        out.append(dict(basis=basis, period_from=start_m, period_to=asof, starting_book_mrr=round(base, 2), starting_accounts=int(len(s0)), ending_mrr_same_accounts=round(float(s1.sum()), 2), churned_accounts=churned, GRR=round(grr * 100, 1) if base else None, NRR=round(nrr * 100, 1) if base else None))
    return pd.DataFrame(out)


def cohorts(cmp, asof):
    """Revenue retained by start quarter at age 12 months, schedule basis vs supported basis."""
    rows = []
    first = cmp[cmp["sched_mrr"] > 0].groupby("key")["month"].min()
    for key, fm in first.items():
        q = pd.Period(fm, freq="M").asfreq("Q").strftime("%YQ%q")
        m12 = (pd.Period(fm, freq="M") + 12).strftime("%Y-%m")
        if pd.Period(m12, freq="M") > pd.Period(asof, freq="M"):
            continue
        g = cmp[cmp["key"] == key].set_index("month")
        rows.append(dict(cohort=q, key=key, start_sched=g.loc[fm, "sched_mrr"], m12_sched=g["sched_mrr"].get(m12, 0.0), start_sup=g.loc[fm, "supported_mrr"], m12_sup=g["supported_mrr"].get(m12, 0.0)))
    if not rows:
        return pd.DataFrame(columns=["cohort", "accounts", "revenue_retained_12m_schedule_pct", "revenue_retained_12m_supported_pct"])
    d = pd.DataFrame(rows)
    c = d.groupby("cohort").agg(accounts=("key", "count"), s0=("start_sched", "sum"), s12=("m12_sched", "sum"), p0=("start_sup", "sum"), p12=("m12_sup", "sum")).reset_index()
    c["revenue_retained_12m_schedule_pct"] = (c["s12"] / c["s0"] * 100).round(1)
    c["revenue_retained_12m_supported_pct"] = (c["p12"] / c["p0"].replace(0, float("nan")) * 100).round(1)
    return c[["cohort", "accounts", "revenue_retained_12m_schedule_pct", "revenue_retained_12m_supported_pct"]]


# ---------- 8 report ----------
def money(x):
    return f"{x:,.0f}"


def write_report(out, man, cmp, acc, ret, coh, match, lines_df, asof, rules):
    cur = cmp[cmp["month"] == asof]
    reported = float(cur["sched_mrr"].sum()) * 12
    supported = float(cur["supported_mrr"].sum()) * 12
    unresolved = reported - supported
    bad = acc[acc["dominant_cause"] != "supported"]
    by_cause = bad.groupby("dominant_cause").agg(accounts=("key", "count"), unresolved_arr=("asof_unresolved_mrr", lambda s: s.sum() * 12)).sort_values("unresolved_arr", ascending=False)
    unknown = int((lines_df["kind"] == "unknown_type").sum())
    nop = int(lines_df["no_period"].sum())
    L = []
    L += [f"# Pilot findings, as of {asof}", "", f"Inputs: schedule `{man['inputs']['schedule']['file']}` ({man['inputs']['schedule']['rows']} rows), invoices `{man['inputs']['invoices']['file']}` ({man['inputs']['invoices']['rows']} lines). Hashes in manifest.json. Kit {KIT_VERSION}.", ""]
    L += ["## Totals", "", "| | ARR |", "|---|---:|", f"| Reported on the schedule | {money(reported)} |", f"| Supported by invoices | {money(supported)} |", f"| Unresolved, needs review | {money(unresolved)} |", f"| Accounts on the schedule | {int((cur['sched_mrr'] > 0).sum())} |", f"| Accounts with an exception in the last {rules['months_for_retention']} months | {len(bad)} |", ""]
    L += ["## Exceptions by cause", "", "| cause | accounts | unresolved ARR |", "|---|---:|---:|"]
    for cause, r in by_cause.iterrows():
        L.append(f"| {cause} | {int(r['accounts'])} | {money(r['unresolved_arr'])} |")
    L += ["", "What each cause means: supported, the invoices back the schedule within tolerance. over_stated, the schedule carries more than the invoices show. under_stated, the invoices show more than the schedule. timing, the same money lands a month earlier or later. credit_or_rebill, a credit memo or rebill touched the month. duplicate_invoice, the same invoice id appears twice. ramp_or_proration, the first months bill below the schedule. classification, a non recurring or untyped line sits where recurring revenue is expected. unmatched_schedule_only, no invoices found for the account. unmatched_invoice_only, invoices with no account on the schedule.", ""]
    L += ["## Retention, three ways", "", "| basis | from | to | starting book MRR | accounts | GRR | NRR |", "|---|---|---|---:|---:|---:|---:|"]
    for _, r in ret.iterrows():
        L.append(f"| {r['basis']} | {r['period_from']} | {r['period_to']} | {money(r['starting_book_mrr'])} | {r['starting_accounts']} | {r['GRR']} | {r['NRR']} |")
    L += ["", "Schedule is what the company reports. Invoices is what the billing lines support on their own. Supported keeps the schedule figure wherever the invoices back it and drops to the invoice figure where they do not.", ""]
    if len(coh):
        L += ["## Cohorts, revenue retained at 12 months", "", "| cohort | accounts | schedule | supported |", "|---|---:|---:|---:|"]
        for _, r in coh.iterrows():
            L.append(f"| {r['cohort']} | {r['accounts']} | {r['revenue_retained_12m_schedule_pct']} | {r['revenue_retained_12m_supported_pct']} |")
        L.append("")
    L += ["## Accounts to review first", "", "| account | schedule MRR | invoice MRR | unresolved MRR | cause | months not supported |", "|---|---:|---:|---:|---|---:|"]
    for _, r in bad.head(25).iterrows():
        nm = r["customer_name"] or r["customer_id"] or r["key"]
        L.append(f"| {nm} | {money(r['asof_schedule_mrr'])} | {money(r['asof_invoice_mrr'])} | {money(r['asof_unresolved_mrr'])} | {r['dominant_cause']} | {r['months_not_supported']} |")
    L += ["", "## Matching", "", f"Exact id: {int((match['method'] == 'exact_id').sum())}. Normalised name: {int((match['method'] == 'normalised_name').sum())}. Ambiguous: {int((match['method'] == 'ambiguous_name').sum())}. Schedule only: {int((match['method'] == 'unmatched_schedule_only').sum())}. Invoices only: {int((match['method'] == 'unmatched_invoice_only').sum())}. Full table in match_table.csv.", ""]
    L += ["## What the files cannot support", "", f"Invoice lines with no type the kit could classify: {unknown}. Lines with no service period (spread to the invoice month instead): {nop}. Mapping warnings: {len(man['mapping_warnings'])}.", "", "The kit does not read contracts, order forms, the general ledger or bank records. Where the schedule and the invoices disagree, the kit shows the disagreement and the lines behind it. Which figure is right is a question for the company's finance lead, and in a transaction for the diligence team. Nothing here is an accounting opinion.", ""]
    L += ["## Questions for finance", ""]
    for cause in by_cause.index:
        q = {"over_stated": "Which accounts carry schedule MRR above the invoices, and is there a contract or a billing gap behind each one?", "under_stated": "Which accounts bill above the schedule, and is the schedule stale or the billing wrong?", "timing": "Is invoice timing a month off by design (billing in arrears) or by accident?", "credit_or_rebill": "What caused each credit memo, and was the rebill for the same period?", "duplicate_invoice": "Are the duplicate invoice ids a true double bill or an export artefact?", "ramp_or_proration": "Do the first months bill below the schedule by contract (ramp) or by proration, and should the schedule show the ramp?", "classification": "Which untyped or one off lines are recurring by contract and which are not?", "unmatched_schedule_only": "Where are the invoices for the accounts with none?", "unmatched_invoice_only": "Which billed accounts are missing from the schedule, and why?"}.get(cause)
        if q:
            L.append(f"* {q}")
    L.append("")
    open(os.path.join(out, "report.md"), "w").write("\n".join(L))
    with pd.ExcelWriter(os.path.join(out, "pilot.xlsx"), engine="openpyxl") as xw:
        pd.DataFrame([{"reported_arr": reported, "supported_arr": supported, "unresolved_arr": unresolved, "asof": asof}]).to_excel(xw, sheet_name="Summary", index=False)
        by_cause.reset_index().to_excel(xw, sheet_name="Exceptions by cause", index=False)
        acc.to_excel(xw, sheet_name="Accounts", index=False)
        ret.to_excel(xw, sheet_name="Retention", index=False)
        coh.to_excel(xw, sheet_name="Cohorts", index=False)
        match.to_excel(xw, sheet_name="Match", index=False)
        cmp.drop(columns=[c for c in ["start", "end"] if c in cmp.columns]).to_excel(xw, sheet_name="Customer month", index=False)


# ---------- 9 supported export ----------
def supported_export(cmp, out, asof):
    """Indexxero export shape: account_id, created_at, status, churned_at, observed_at, arr. One row per account per month."""
    d = cmp[cmp["supported_mrr"] > 0].copy()
    first = d.groupby("key")["month"].min().to_dict()
    last = d.groupby("key")["month"].max().to_dict()
    starts = cmp.dropna(subset=["start"]).groupby("key")["start"].min().to_dict() if "start" in cmp.columns else {}
    rows = []
    for _, r in d.iterrows():
        k = r["key"]
        created = starts.get(k)
        created = created.strftime("%Y-%m-%d") if created is not None and not pd.isna(created) else pd.Period(first[k], freq="M").to_timestamp(how="start").strftime("%Y-%m-%d")
        churned = (pd.Period(last[k], freq="M") + 1).to_timestamp(how="start").strftime("%Y-%m-%d") if pd.Period(last[k], freq="M") < pd.Period(asof, freq="M") else ""
        rows.append(dict(account_id=k, created_at=created, status="churned" if churned else "active", churned_at=churned, observed_at=pd.Period(r["month"], freq="M").to_timestamp(how="end").strftime("%Y-%m-%d"), arr=round(r["supported_mrr"] * 12, 2)))
    cols = ["account_id", "created_at", "status", "churned_at", "observed_at", "arr"]
    (pd.DataFrame(rows, columns=cols).sort_values(["account_id", "observed_at"]) if rows else pd.DataFrame(columns=cols)).to_csv(os.path.join(out, "supported_export.csv"), index=False)


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--schedule", required=True); ap.add_argument("--invoices", required=True); ap.add_argument("--map", required=True)
    ap.add_argument("--asof", required=True, help="YYYY-MM, the month the figures are stated as of"); ap.add_argument("--out", required=True)
    ap.add_argument("--profile", action="store_true", help="stop after the manifest and the profile; no numbers computed")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    mapping = json.load(open(args.map))
    rules = mapping["rules"]
    sched_raw, sched, miss1 = load_mapped(args.schedule, "schedule", mapping["schedule"])
    inv_raw, inv, miss2 = load_mapped(args.invoices, "invoices", mapping["invoices"])
    man = step_manifest(args, sched_raw, inv_raw, miss1 + miss2)
    json.dump(man, open(os.path.join(args.out, "manifest.json"), "w"), indent=2)
    open(os.path.join(args.out, "profile.md"), "w").write(step_profile(sched, inv, man))
    if args.profile:
        print("profile only:", ", ".join(sorted(os.listdir(args.out))))
        for w in man["mapping_warnings"]:
            print("note:", w)
        return
    sched_m = schedule_monthly(sched, mapping["schedule"].get("format", "snapshot"), args.asof)
    lines_df, inv_m, inv_names = rebuild(inv, rules)
    lines_df.to_csv(os.path.join(args.out, "invoice_lines_classified.csv"), index=False)
    inv_m.to_csv(os.path.join(args.out, "invoice_mrr_monthly.csv"), index=False)
    sched_names = sched_m.groupby("key").agg(cid=("cid", "first"), cname=("cname", "first")).reset_index()
    match = match_customers(sched_names, inv_names)
    match.to_csv(os.path.join(args.out, "match_table.csv"), index=False)
    cmp = compare(sched_m, inv_m, match, rules, args.asof, rules["ramp_months"], inv_names)
    cmp.to_csv(os.path.join(args.out, "compare_customer_month.csv"), index=False)
    cmp[~cmp["bucket"].isin(["supported", "no_activity"])].to_csv(os.path.join(args.out, "exceptions.csv"), index=False)
    acc = accounts_view(cmp, args.asof, rules["months_for_retention"])
    acc.to_csv(os.path.join(args.out, "accounts.csv"), index=False)
    ret = retention(cmp, args.asof, rules["months_for_retention"])
    ret.to_csv(os.path.join(args.out, "retention.csv"), index=False)
    coh = cohorts(cmp, args.asof)
    coh.to_csv(os.path.join(args.out, "cohorts.csv"), index=False)
    write_report(args.out, man, cmp, acc, ret, coh, match, lines_df, args.asof, rules)
    supported_export(cmp, args.out, args.asof)
    print("done:", ", ".join(sorted(os.listdir(args.out))))
    for w in man["mapping_warnings"]:
        print("warning:", w)


if __name__ == "__main__":
    main()
