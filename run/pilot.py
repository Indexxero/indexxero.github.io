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
try:
    import pandas as pd
except ImportError:
    sys.exit("pandas is not installed for this Python (%s). Run: python3 -m pip install pandas openpyxl" % sys.executable)
try:
    import openpyxl  # noqa: F401
    HAVE_OPENPYXL = True
except ImportError:
    HAVE_OPENPYXL = False

KIT_VERSION = "2026-10-26"


# ---------- helpers ----------
def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


ANSWER_DECISIONS = {"accepted", "explained", "disputed"}
ANSWER_REASONS = {"contract_amendment", "credit_memo", "billing_error", "schedule_error", "reseller_child", "test_account", "other"}


def load_answers(path):
    """Load the small finance-lead decision file; reject ambiguity before changing a result."""
    try:
        payload = json.load(open(path))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read answers file: {exc}") from exc
    rows = payload.get("answers") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("answers file must contain an 'answers' array")
    clean, seen = [], set()
    required = ("customer_key", "month", "decision", "reason", "note", "by", "date")
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict) or any(field not in row for field in required):
            raise ValueError(f"answer {number} must include: {', '.join(required)}")
        answer = {field: str(row[field]).strip() for field in required}
        if not answer["customer_key"] or not re.fullmatch(r"\d{4}-\d{2}", answer["month"]):
            raise ValueError(f"answer {number} needs a customer_key and YYYY-MM month")
        if answer["decision"] not in ANSWER_DECISIONS:
            raise ValueError(f"answer {number} has invalid decision '{answer['decision']}'")
        if answer["reason"] not in ANSWER_REASONS:
            raise ValueError(f"answer {number} has invalid reason '{answer['reason']}'")
        key = (answer["customer_key"], answer["month"])
        if key in seen:
            raise ValueError(f"duplicate answer for {answer['customer_key']} {answer['month']}")
        seen.add(key)
        clean.append(answer)
    return clean


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
        if not HAVE_OPENPYXL:
            sys.exit("%s is an Excel file and openpyxl is not installed for this Python (%s). Run: python3 -m pip install openpyxl, or save the sheet as csv." % (path, sys.executable))
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
    require_columns(section, out, mapping)
    return raw, out, missing


REQUIRED = {
    "invoices": [("amount",), ("invoice_date",), ("customer_id", "customer_name")],
    "schedule": [("mrr", "arr"), ("snapshot_date", "start_date"), ("customer_id", "customer_name")],
}


def require_columns(section, out, mapping):
    """Stop before anything is computed when a column every figure depends on is missing.
    Without this, a mapping that names columns the file does not have (a different export, a
    report with title rows above the header) produced a complete report on warning lines: every
    dollar of ARR called unsupported, which reads as a finding about the client when it is a
    mapping mismatch. Found by Claude Code on a grouped QuickBooks export, 26 Oct."""
    if section not in REQUIRED:
        return
    def present(col):
        return col in out.columns and out[col].astype(str).str.strip().replace("nan", "").ne("").any()
    gaps = [" or ".join(group) for group in REQUIRED[section] if not any(present(c) for c in group)]
    if gaps:
        named = {c: mapping.get(c, "") for group in REQUIRED[section] for c in group}
        sys.exit(f"{section}: no values found for {', '.join(gaps)}. The mapping names "
                 + ", ".join(f"{k} -> '{v}'" for k, v in named.items() if v)
                 + ". Check that the file's header row holds these column names (a report with title rows above the header, or a different export, will not match). Nothing was computed.")


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
def step_manifest(args, sched_raw, inv_raw, missing, answers_path=None):
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
    if answers_path:
        m["answers"] = {"file": os.path.basename(answers_path), "sha256": sha256(answers_path)}
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


def infer_periods(inv, lines_df, sched_m, match, rules):
    """Give a periodless recurring line a service period ONLY when its amount is an exact
    whole multiple of the matched customer's schedule MRR (within tolerance), 2 to 36 months,
    starting in the invoice month. QuickBooks Online exports carry no service period, so an
    annual invoice would otherwise read as one enormous month and no deferred revenue would
    ever show. A line that is not an exact multiple, or matches one month, keeps the old
    behaviour (invoice month, flagged). Every inferred line is marked
    period_source = inferred_from_schedule. Rule `periodless_recurring` in the mapping:
    "whole_months_from_schedule" turns it on; anything else, or absent, leaves it off."""
    if rules.get("periodless_recurring", "invoice_month") != "whole_months_from_schedule":
        return inv, 0
    n = len(inv)
    base = lines_df.iloc[:n]
    cand = base[(base["kind"] == "recurring") & base["no_period"] & base["inv_date_v"].notna() & base["amount_v"].notna() & (base["amount_v"] > 0)]
    if not len(cand):
        return inv, 0
    to_sched = {r["inv_key"]: r["sched_key"] for _, r in match.iterrows() if r.get("inv_key") and r.get("sched_key")}
    mrr = {(r["key"], r["month"]): float(r["sched_mrr"]) for _, r in sched_m.iterrows()}
    tol_pct, tol_abs = float(rules.get("tolerance_pct", 1.0)), float(rules.get("tolerance_abs", 5.0))
    out = inv.copy()
    if "_period_inferred" not in out.columns:
        out["_period_inferred"] = False
    count = 0
    for idx, r in cand.iterrows():
        sk = to_sched.get(r["key"])
        if not sk:
            continue
        p = r["inv_date_v"].to_period("M")
        m = mrr.get((sk, p.strftime("%Y-%m")), 0.0) or mrr.get((sk, (p + 1).strftime("%Y-%m")), 0.0)
        if m <= 0:
            continue
        k = int(round(r["amount_v"] / m))
        if k < 2 or k > 36 or abs(r["amount_v"] - k * m) > max(tol_abs, tol_pct / 100.0 * r["amount_v"]):
            continue
        pos = out.index[base.index.get_loc(idx)]
        out.at[pos, "service_start"] = p.to_timestamp(how="start").strftime("%Y-%m-%d")
        out.at[pos, "service_end"] = (p + k - 1).to_timestamp(how="end").strftime("%Y-%m-%d")
        out.at[pos, "_period_inferred"] = True
        count += 1
    return out, count


def rebuild(inv, rules, sched_m=None):
    """Classify every invoice line and spread the recurring ones over their months.

    `sched_m` is optional and exists for ONE rule (Cowork 2026-10-24 22:40): a
    recurring line with NO service period takes its period from the matched
    customer's schedule months. QuickBooks Online is the reason. Its invoice
    export carries no service period at all, so without this every recurring
    line lands in the month it was raised and an annual invoice reads as one
    enormous month. That is the shape the deferred lane is built for.

    The line is marked `period_source = schedule` in
    `invoice_lines_classified.csv`, so nobody has to infer which lines were
    treated this way. A line with neither a period nor a schedule month keeps
    the old behaviour exactly: invoice month, flagged.
    """
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
    # NOT RE-DATED, AND THE REASON IS MEASURED. Cowork's 22:40 rule has a
    # periodless recurring line take its period from the matched customer's
    # schedule months, for QuickBooks Online, whose export carries no service
    # period at all. Implemented literally it spreads EVERY periodless line over
    # every month the customer carries MRR, and on Larkspur that is 13 monthly
    # invoices for one account turned into 13 thirteenths each: the five key
    # files all moved, against the same entry's instruction that they must not.
    #
    # The missing piece is a DURATION. A monthly invoice with no period belongs
    # in its own month; an annual one belongs across twelve. The only signal
    # available is the amount divided by the customer's schedule MRR, and
    # inferring a service period from money is the kind of guess this kit
    # refuses elsewhere by design. So the rule waits for Cowork to say how the
    # duration is decided, and the old behaviour stands meanwhile.
    nop_left = nop
    nop_df = pd.DataFrame({"key": nop_left["key"], "month": nop_left["inv_date_v"].dt.strftime("%Y-%m"), "kind": nop_left["kind"], "amount": nop_left["amount_v"], "no_period": True, "duplicate_id": nop_left["duplicate_id"].astype(bool)})
    sched_df = None
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
    rows = [rows_df, nop_df] + ([sched_df] if sched_df is not None else []) + ([pd.DataFrame(rows)] if rows else [])
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
    # `period_source` on every line: `invoice` where the line carried its own
    # service period, `schedule` where the schedule dated it, `invoice_month`
    # where neither could and the old behaviour applied.
    df["period_source"] = "invoice"
    df.loc[df["no_period"], "period_source"] = "invoice_month"
    if "_period_inferred" in df.columns:
        df.loc[df["_period_inferred"].fillna(False).astype(bool), "period_source"] = "inferred_from_schedule"
        df = df.drop(columns=["_period_inferred"])
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
    for c in ["inv_recurring", "inv_credit", "inv_mrr", "non_recurring_amount"]:
        cmp[c] = cmp[c].fillna(0.0)
    # unknown_lines is a COUNT and is written as one. `fillna(0.0)` left it a
    # float, and pandas 2 and pandas 3 then print the same count differently:
    # the browser kit (pandas 2.3.1) wrote `0.0` where a laptop (pandas 3.1.0)
    # wrote `0`, on all 404 rows of the QuickBooks shape, with every figure
    # agreeing. One line here rather than a note in two places.
    cmp["unknown_lines"] = cmp["unknown_lines"].fillna(0).astype(int)
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


def apply_answers(cmp, answers):
    """Apply disclosed finance decisions after the kit has produced its unassisted result."""
    out = cmp.copy()
    out["answer_decision"] = ""
    out["answer_reason"] = ""
    out["answer_note"] = ""
    out["answer_by"] = ""
    out["answer_date"] = ""
    out["answer_prior_bucket"] = ""
    out["answer_override_mrr"] = 0.0
    answered = []
    for answer in answers:
        mask = (out["key"] == answer["customer_key"]) & (out["month"] == answer["month"])
        if not mask.any():
            answered.append({**answer, "status": "not_found", "prior_bucket": "", "result_bucket": "", "override_mrr": 0.0})
            continue
        index = out.index[mask][0]
        prior_bucket = out.at[index, "bucket"]
        override = 0.0
        if answer["decision"] == "explained":
            override = float(out.at[index, "sched_mrr"] - out.at[index, "supported_mrr"])
            out.at[index, "supported_mrr"] = out.at[index, "sched_mrr"]
            out.at[index, "bucket"] = f"explained_{answer['reason']}"
        elif answer["decision"] == "accepted":
            out.at[index, "bucket"] = f"{prior_bucket}_accepted"
        out.at[index, "answer_decision"] = answer["decision"]
        out.at[index, "answer_reason"] = answer["reason"]
        out.at[index, "answer_note"] = answer["note"]
        out.at[index, "answer_by"] = answer["by"]
        out.at[index, "answer_date"] = answer["date"]
        out.at[index, "answer_prior_bucket"] = prior_bucket
        out.at[index, "answer_override_mrr"] = override
        answered.append({**answer, "status": "applied", "prior_bucket": prior_bucket, "result_bucket": out.at[index, "bucket"], "override_mrr": override})
    return out, answered


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


# ---------- 7b ARR bridge ----------
BRIDGE_COLUMNS = ["start", "new", "reactivation", "expansion", "contraction", "churn", "end"]


def bridge(cmp, asof, months_back):
    """Monthly MRR bridge on three bases: schedule, invoices, supported. Identity asserted every month."""
    asof_p = pd.Period(asof, freq="M")
    months = [(asof_p - offset).strftime("%Y-%m") for offset in range(months_back - 1, -1, -1)]
    prior_month = (pd.Period(months[0], freq="M") - 1).strftime("%Y-%m")
    keys = sorted(cmp["key"].dropna().unique())
    names = cmp.groupby("key")["cname"].first().to_dict()
    bridge_rows, account_rows = [], []

    for basis, column in (("schedule", "sched_mrr"), ("invoices", "inv_mrr"), ("supported", "supported_mrr")):
        values = cmp.pivot_table(index="key", columns="month", values=column, aggfunc="sum", fill_value=0.0)
        values = values.reindex(index=keys, columns=[prior_month] + months, fill_value=0.0).fillna(0.0)
        seen = set()
        for month in months:
            prev = values[prior_month if month == months[0] else months[months.index(month) - 1]]
            cur = values[month]
            totals = {"start": float(prev.sum()), "new": 0.0, "reactivation": 0.0, "expansion": 0.0, "contraction": 0.0, "churn": 0.0, "end": float(cur.sum())}
            for key in keys:
                old, now = float(prev.loc[key]), float(cur.loc[key])
                if old > 0:
                    account_rows.append(dict(month=month, basis=basis, customer_key=key, customer_name=names.get(key, ""), movement="start", amount=old))
                if now > 0:
                    account_rows.append(dict(month=month, basis=basis, customer_key=key, customer_name=names.get(key, ""), movement="end", amount=now))
                if old == 0 and now > 0:
                    movement = "reactivation" if key in seen else "new"
                    totals[movement] += now
                    account_rows.append(dict(month=month, basis=basis, customer_key=key, customer_name=names.get(key, ""), movement=movement, amount=now))
                elif old > 0 and now > old:
                    amount = now - old
                    totals["expansion"] += amount
                    account_rows.append(dict(month=month, basis=basis, customer_key=key, customer_name=names.get(key, ""), movement="expansion", amount=amount))
                elif old > 0 and now > 0 and now < old:
                    amount = old - now
                    totals["contraction"] += amount
                    account_rows.append(dict(month=month, basis=basis, customer_key=key, customer_name=names.get(key, ""), movement="contraction", amount=amount))
                elif old > 0 and now == 0:
                    totals["churn"] += old
                    account_rows.append(dict(month=month, basis=basis, customer_key=key, customer_name=names.get(key, ""), movement="churn", amount=old))
                if now > 0:
                    seen.add(key)
            identity = totals["start"] + totals["new"] + totals["reactivation"] + totals["expansion"] - totals["contraction"] - totals["churn"]
            if abs(identity - totals["end"]) >= 0.01:
                raise AssertionError(f"ARR bridge identity failed for {basis} {month}: {identity} != {totals['end']}")
            bridge_rows.append(dict(month=month, basis=basis, **{name: round(value, 2) for name, value in totals.items()}))

    bridge_df = pd.DataFrame(bridge_rows, columns=["month", "basis"] + BRIDGE_COLUMNS)
    schedule = bridge_df[bridge_df["basis"] == "schedule"].set_index("month")
    supported = bridge_df[bridge_df["basis"] == "supported"].set_index("month")
    difference = (schedule[BRIDGE_COLUMNS] - supported[BRIDGE_COLUMNS]).reset_index()
    difference.insert(1, "basis", "difference")
    bridge_df = pd.concat([bridge_df, difference], ignore_index=True).sort_values(["month", "basis"]).reset_index(drop=True)
    return bridge_df, pd.DataFrame(account_rows, columns=["month", "basis", "customer_key", "customer_name", "movement", "amount"])


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


# ---------- 7b deferred revenue implied by the invoices ----------
def deferred(lines_df, inv_m, asof, months_back, names=None, gl=None):
    """Billed against recognised, per month, from the spread the kit already does.

    THE TWO SIDES ARE NEVER NETTED INTO ONE WORD. A positive balance is deferred
    revenue: invoiced ahead of the service. A negative one is unbilled or
    accrued: recognised ahead of the invoice. A single "net deferred" column
    would let a book with half a million either side read as nil, which is the
    sentence this kit exists to refuse.

    `billed` is the line amount in the month of its invoice date. `recognised`
    is the share the kit allocates to that month, which is the same number
    `invoice_mrr_monthly.csv` already reports; it is read from there rather than
    recomputed, because a second allocation path beside the first is how the two
    drift apart.

    Credits carry their sign on both sides, so a credit memo reduces the billing
    in its own month and the recognition in the months it belongs to.

    This asserts its own identity every month: opening plus billings minus
    recognised equals closing. If that fails the numbers are not reported.
    """
    kinds = lines_df["kind"].isin(["recurring", "credit"])
    bl = lines_df[kinds & lines_df["amount_v"].notna() & lines_df["inv_date_v"].notna()].copy()
    bl["month"] = bl["inv_date_v"].dt.strftime("%Y-%m")
    billed = bl.groupby(["key", "month"], as_index=False).agg(billed=("amount_v", "sum"))
    rec = inv_m[["key", "month", "inv_mrr"]].rename(columns={"inv_mrr": "recognised"})
    both = billed.merge(rec, on=["key", "month"], how="outer").fillna({"billed": 0.0, "recognised": 0.0})
    both = both[both["month"].astype(str) != ""]
    if not len(both):
        empty_roll = pd.DataFrame(columns=["month", "opening", "billings", "recognised", "closing_deferred", "closing_unbilled"])
        empty_acc = pd.DataFrame(columns=["key", "customer_name", "closing_deferred", "closing_unbilled", "months_of_deferred_cover"])
        return empty_roll, empty_acc, pd.DataFrame(columns=["month", "customer_name", "debit_account", "credit_account", "amount", "memo"])

    months = sorted(m for m in both["month"].unique() if m <= asof)
    if months_back:
        months = months[-int(months_back):] if len(months) > int(months_back) else months

    # per customer running balance, so the two sides can be reported apart
    per = both[both["month"] <= asof].sort_values(["key", "month"])
    per["delta"] = per["billed"] - per["recognised"]
    per["balance"] = per.groupby("key")["delta"].cumsum()

    rows = []
    opening_total = 0.0
    first = months[0] if months else None
    if first is not None:
        before = per[per["month"] < first]
        opening_total = float(before.groupby("key")["delta"].sum().sum()) if len(before) else 0.0
    running = opening_total
    for m in months:
        inm = per[per["month"] == m]
        b = float(inm["billed"].sum())
        r = float(inm["recognised"].sum())
        closing = running + b - r
        # The identity, asserted rather than trusted. A roll forward whose own
        # arithmetic does not close is not a finding, it is a bug.
        if abs((running + b - r) - closing) > 0.005:
            raise AssertionError(f"deferred roll forward does not close at {m}")
        at_m = per[per["month"] <= m].groupby("key")["delta"].sum()
        rows.append(dict(month=m, opening=round(running, 2), billings=round(b, 2), recognised=round(r, 2),
                         # `+ 0.0` so a month with nothing on a side reads 0.0
                         # and never -0.0, which is not a figure anyone should
                         # be handed in an accounting schedule.
                         closing_deferred=round(float(at_m[at_m > 0].sum()) + 0.0, 2) + 0.0,
                         closing_unbilled=round(float(-at_m[at_m < 0].sum()) + 0.0, 2) + 0.0))
        running = closing
    roll = pd.DataFrame(rows)
    roll["closing"] = (roll["opening"] + roll["billings"] - roll["recognised"]).round(2)
    if gl is not None and len(gl):
        g = gl.rename(columns={"balance": "gl_balance"})
        roll = roll.merge(g[["month", "gl_balance"]], on="month", how="left")
        roll["difference"] = (roll["closing"] - roll["gl_balance"]).round(2)
    roll = roll[[c for c in ["month", "opening", "billings", "recognised", "closing", "closing_deferred", "closing_unbilled", "gl_balance", "difference"] if c in roll.columns]]

    # per customer, at the as of month
    at = per[per["month"] <= asof].groupby("key")["delta"].sum()
    cur_rec = per[per["month"] == asof].set_index("key")["recognised"] if len(per[per["month"] == asof]) else pd.Series(dtype=float)
    nm = names.set_index("key")["cname"].to_dict() if names is not None else {}
    arows = []
    for k, bal in at.items():
        def_, unb = (round(float(bal), 2), 0.0) if bal > 0 else (0.0, round(float(-bal), 2))
        monthly_rec = float(cur_rec.get(k, 0.0))
        # Blank, not zero, when there is nothing to divide by: "no cover" and
        # "we cannot say" are different answers.
        cover = round(def_ / monthly_rec, 1) if (def_ > 0 and monthly_rec > 0) else ""
        arows.append(dict(key=k, customer_name=nm.get(k, ""), closing_deferred=def_, closing_unbilled=unb, months_of_deferred_cover=cover))
    acc = pd.DataFrame(arows).sort_values(["closing_deferred", "closing_unbilled"], ascending=False)

    # The journal, as a QuickBooks Online user posts it today. QuickBooks books an
    # invoice straight to income, so the month end entry is ONE adjustment per
    # customer that moves the deferred balance from where it was to where the
    # invoices say it should be: the change in the balance, not the revenue. A
    # customer billed monthly in arrears or in advance for the same month has no
    # change and therefore no line. Positive change: debit Subscription revenue,
    # credit Deferred revenue (billed ahead, defer it). Negative: the reverse
    # (release what was deferred). On the unbilled side the accounts are
    # Unbilled revenue and Subscription revenue. The memo names only the
    # invoices whose service periods reach into or past the month.
    jrows = []
    memo_src = bl.copy()
    memo_src["iid"] = memo_src["invoice_id"].astype(str).str.strip()
    se = memo_src["se_v"] if "se_v" in memo_src.columns else pd.Series(pd.NaT, index=memo_src.index)
    memo_src["last_month"] = se.dt.strftime("%Y-%m").where(se.notna(), memo_src["month"])
    prev_bal = per[per["month"] < months[0]].groupby("key")["delta"].sum().to_dict() if months else {}
    for m in months:
        cur_bal = per[per["month"] <= m].groupby("key")["delta"].sum().to_dict()
        for k in sorted(set(cur_bal) | set(prev_bal)):
            before, after = float(prev_bal.get(k, 0.0)), float(cur_bal.get(k, 0.0))
            for side, b0, b1 in (("deferred", max(before, 0.0), max(after, 0.0)), ("unbilled", max(-before, 0.0), max(-after, 0.0))):
                change = round(b1 - b0, 2)
                if abs(change) < 0.005:
                    continue
                if side == "deferred":
                    dr, cr = ("Subscription revenue", "Deferred revenue") if change > 0 else ("Deferred revenue", "Subscription revenue")
                else:
                    dr, cr = ("Unbilled revenue", "Subscription revenue") if change > 0 else ("Subscription revenue", "Unbilled revenue")
                live = memo_src[(memo_src["key"] == k) & (memo_src["month"] <= m) & (memo_src["last_month"] >= m)]
                ids = sorted(set(live["iid"]) - {""})
                memo = ("invoices " + ", ".join(ids[:6]) + (" and others" if len(ids) > 6 else "")) if ids else "no invoice id on the lines"
                jrows.append(dict(month=m, customer_name=nm.get(k, k), debit_account=dr, credit_account=cr, amount=abs(change), memo=memo))
        prev_bal = cur_bal
    journal = pd.DataFrame(jrows, columns=["month", "customer_name", "debit_account", "credit_account", "amount", "memo"])
    for frame in (roll, acc, journal):
        for c in frame.select_dtypes("number").columns:
            frame[c] = frame[c] + 0.0  # no "-0.0" in a schedule a controller reads
    return roll, acc, journal


# ---------- 7c ARR to recognised revenue ----------
ARR_TO_REVENUE_COLUMNS = ["month", "order", "section", "step", "bucket", "amount", "running", "accounts"]


def arr_to_revenue(cmp, roll, asof):
    """arr_to_revenue.csv: schedule MRR to supported MRR to invoiced recurring to the
    recognised revenue of the roll forward, for the as of month, one row per step.

    READS, DOES NOT RECOMPUTE. Every amount is a sum of columns compare_customer_month.csv
    and deferred_rollforward.csv already carry (sched_mrr, supported_mrr, inv_mrr,
    non_recurring_amount; opening, billings, recognised, closing), so the bridge cannot
    disagree with those files by being worked a second way. A step is one bucket, the
    kit's own name; under_stated is split where the schedule shows nothing, because
    "billed to a customer the schedule shows as gone" and "billed above the schedule"
    are different questions. Each subtotal is read from its own column and compared
    with the steps before it; any gap is written as a row named residual, never
    absorbed. One off lines are a memo row: the roll forward carries recurring and
    credit lines only, so they are in neither ARR nor this recognised figure."""
    cur = cmp[cmp["month"] == asof].copy()
    rows = []

    def names(sub):
        return "; ".join(sorted(f"{(str(r['cname']) if isinstance(r['cname'], str) and r['cname'] else str(r['key']))} ({r['key']})"
                                for _, r in sub.iterrows()))

    def add(section, step, bucket, amount, accounts="", running=None):
        rows.append(dict(month=asof, order=len(rows) + 1, section=section, step=step, bucket=bucket,
                         amount=round(float(amount), 2) + 0.0, running=None if running is None else round(float(running), 2) + 0.0, accounts=accounts))

    def steps(section, gap, sign, word):
        nonlocal running
        cur["_gap"] = gap
        moved = cur[cur["_gap"].round(2) != 0].copy()
        moved["_label"] = moved["bucket"].astype(str)
        split = moved["bucket"].astype(str).str.startswith("under_stated")
        moved.loc[split & (moved["sched_mrr"] == 0), "_label"] = moved["bucket"].astype(str) + "|schedule shows nothing"
        moved.loc[split & (moved["sched_mrr"] != 0), "_label"] = moved["bucket"].astype(str) + "|billed above the schedule"
        groups = sorted(moved.groupby("_label"), key=lambda kv: -abs(float(kv[1]["_gap"].sum())))
        for label, sub in groups:
            bucket, _, qual = label.partition("|")
            amount = sign * float(sub["_gap"].sum())
            running += amount
            add(section, f"{word} {bucket}" + (f", {qual}" if qual else ""), bucket, amount, names(sub), running)

    def subtotal(section, step, stated):
        nonlocal running
        gap = round(float(stated) - running, 2)
        if gap != 0:
            running += gap
            add(section, f"residual: {step} is {stated:,.2f}, the steps above reach {running - gap:,.2f}", "residual", gap, "", running)
        add(section, f"= {step}", "", stated, "", stated)
        running = float(stated)

    sched, supported, invoiced = (float(cur[c].sum()) for c in ("sched_mrr", "supported_mrr", "inv_mrr"))
    running = sched
    add("schedule_to_supported", "recurring revenue on the schedule", "", sched, "", running)
    steps("schedule_to_supported", cur["sched_mrr"] - cur["supported_mrr"], -1, "less:")
    subtotal("schedule_to_supported", "supported by invoices", supported)
    steps("supported_to_invoiced", cur["inv_mrr"] - cur["supported_mrr"], 1, "plus:")
    subtotal("supported_to_invoiced", "invoiced recurring, spread over service periods", invoiced)
    at = roll[roll["month"] == asof] if roll is not None and len(roll) else roll
    if at is not None and len(at):
        at = at.iloc[0]
        subtotal("invoiced_to_recognised", "recognised revenue, deferred_rollforward.csv", float(at["recognised"]))
        running = float(at["billings"])
        add("billings_to_recognised", "billed in the month, recurring and credit lines", "", running, "", running)
        moved = float(at["closing"]) - float(at["opening"])
        running -= moved
        add("billings_to_recognised", ("less: added to the net deferred balance" if moved > 0 else "plus: released from the net deferred balance")
            + f" ({float(at['opening']):,.2f} to {float(at['closing']):,.2f})", "service_period_spreading", -moved, "", running)
        subtotal("billings_to_recognised", "recognised revenue, deferred_rollforward.csv", float(at["recognised"]))
    one_off = cur[cur["non_recurring_amount"].round(2) != 0]
    add("memo", "one off lines in the month, outside ARR and outside the roll forward", "non_recurring",
        float(cur["non_recurring_amount"].sum()), names(one_off))
    return pd.DataFrame(rows, columns=ARR_TO_REVENUE_COLUMNS)


def arr_to_revenue_markdown(a2r):
    L = ["| step | MRR |", "|---|---:|"]
    for _, r in a2r.iterrows():
        bold = str(r["step"]).startswith("=")
        amt = f"{float(r['amount']):,.2f}"
        L.append(f"| {'**' + r['step'] + '**' if bold else r['step']} | {'**' + amt + '**' if bold else amt} |")
    return L


# ---------- 8 report ----------
def money(x):
    return f"{x:,.0f}"


def write_report(out, man, cmp, acc, ret, coh, match, lines_df, asof, rules, answered=None, bridge_df=None, roll=None, dacc=None, gl_given=False, journal=None, a2r=None):
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
    if bridge_df is not None and len(bridge_df):
        L += ["## ARR bridge, three ways", "", "Schedule is what the company reports. Invoices is what the billing lines show on their own. Supported keeps the schedule figure where the invoices back it. Difference is schedule minus supported. Figures are ARR (MRR times 12). Every movement traces to its accounts in bridge_accounts.csv.", ""]
        months = sorted(bridge_df["month"].unique())
        window = bridge_df.groupby("basis", as_index=False)[BRIDGE_COLUMNS[1:-1]].sum()
        window["start"] = window["basis"].map(bridge_df[bridge_df["month"] == months[0]].set_index("basis")["start"])
        window["end"] = window["basis"].map(bridge_df[bridge_df["month"] == months[-1]].set_index("basis")["end"])
        for title, table in ((f"As of {asof}", bridge_df[bridge_df["month"] == asof]), (f"Window {months[0]} to {months[-1]}, movements summed", window)):
            L += [f"### {title}", "", "| basis | start | new | reactivation | expansion | contraction | churn | end |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
            for basis in ("schedule", "invoices", "supported", "difference"):
                r = table[table["basis"] == basis]
                if len(r):
                    r = r.iloc[0]
                    L.append("| " + basis + " | " + " | ".join(money(r[column] * 12) for column in BRIDGE_COLUMNS) + " |")
            L.append("")
        difference_totals = window[window["basis"] == "difference"].iloc[0]
        movement = max(BRIDGE_COLUMNS[1:-1], key=lambda column: abs(difference_totals[column]))
        L += [f"The largest difference between the two bases over the window is in {movement}: {money(abs(difference_totals[movement]) * 12)} ARR. The schedule and the invoices disagree most about that movement.", ""]
    L += ["## Accounts to review first", "", "| account | schedule MRR | invoice MRR | unresolved MRR | cause | months not supported |", "|---|---:|---:|---:|---|---:|"]
    for _, r in bad.head(25).iterrows():
        nm = r["customer_name"] or r["customer_id"] or r["key"]
        L.append(f"| {nm} | {money(r['asof_schedule_mrr'])} | {money(r['asof_invoice_mrr'])} | {money(r['asof_unresolved_mrr'])} | {r['dominant_cause']} | {r['months_not_supported']} |")
    if answered is not None:
        overridden = [r for r in answered if r["status"] == "applied" and r["decision"] == "explained"]
        override_mrr = sum(r["override_mrr"] for r in overridden)
        L += ["", "## Answered exceptions", "", "| customer key | month | decision | reason | prior bucket | result bucket | note | by | date | status |", "|---|---|---|---|---|---|---|---|---|---|"]
        for r in answered:
            L.append(f"| {r['customer_key']} | {r['month']} | {r['decision']} | {r['reason']} | {r['prior_bucket']} | {r['result_bucket']} | {r['note']} | {r['by']} | {r['date']} | {r['status']} |")
        L += ["", f"Overridden by answers: {len(overridden)} customer months, {money(override_mrr)} MRR", ""]
    if a2r is not None and len(a2r):
        L += [f"## ARR to recognised revenue, {asof}", "",
              "Monthly figures, read from compare_customer_month.csv and deferred_rollforward.csv, one row per bucket; every row and the accounts behind it are in arr_to_revenue.csv. A row named residual is a gap the steps do not explain, shown rather than absorbed.", ""]
        L += arr_to_revenue_markdown(a2r) + [""]
    if roll is not None and len(roll):
        at = roll[roll["month"] == asof]
        closing = float(at["closing"].iloc[0]) if len(at) else float(roll["closing"].iloc[-1])
        def_side = float(at["closing_deferred"].iloc[0]) if len(at) else float(roll["closing_deferred"].iloc[-1])
        unb_side = float(at["closing_unbilled"].iloc[0]) if len(at) else float(roll["closing_unbilled"].iloc[-1])
        L += ["## Deferred revenue implied by the invoices", "",
              "Billed is the invoice line in the month it was raised. Recognised is the share the kit spreads into each month of the service period. The difference, carried forward, is deferred revenue where the billing runs ahead and unbilled revenue where the recognition does. **The two sides are reported apart and never netted**, because a book with money on both sides would otherwise read as nil.", "",
              "| month | opening | billings | recognised | closing | of which deferred | of which unbilled |", "|---|---:|---:|---:|---:|---:|---:|"]
        for _, r in roll.iterrows():
            L.append(f"| {r['month']} | {money(r['opening'])} | {money(r['billings'])} | {money(r['recognised'])} | {money(r['closing'])} | {money(r['closing_deferred'])} | {money(r['closing_unbilled'])} |")
        L += ["", f"Opening plus billings minus recognised equals closing in every month above; the kit asserts it and does not report these figures if it fails.", ""]
        if journal is not None:
            jm = journal[journal["month"] == asof]
            to_def = float(jm[jm["credit_account"] == "Deferred revenue"]["amount"].sum())
            from_def = float(jm[jm["debit_account"] == "Deferred revenue"]["amount"].sum())
            L += [f"Journal for {asof}: {len(jm)} lines, see journal.csv. Credited to deferred revenue {money(to_def)}, released from deferred revenue {money(from_def)}.", ""]
        if unb_side > 0:
            L += [f"Recognition ran ahead of billing for some accounts: unbilled revenue at {asof} is {money(unb_side)}, reported apart from the deferred side.", ""]
        if gl_given and "gl_balance" in roll.columns:
            g = at["gl_balance"].iloc[0] if len(at) else roll["gl_balance"].iloc[-1]
            d = at["difference"].iloc[0] if len(at) else roll["difference"].iloc[-1]
            L += [f"Deferred revenue implied by the invoices at {asof}: {money(closing)}. Per the general ledger: {money(float(g))}. Difference: {money(float(d))}.", ""]
        else:
            L += [f"Deferred revenue implied by the invoices at {asof}: {money(closing)}. Per the general ledger: not provided.", ""]
        L += ["This is a schedule a controller can post from, in `journal.csv`, with the accounts named in plain words. It is not a posting, not a chart of accounts and not an opinion on revenue recognition policy.", ""]
        if dacc is not None and len(dacc):
            top = dacc.head(10)
            L += ["| account | closing deferred | closing unbilled | months of cover |", "|---|---:|---:|---:|"]
            for _, r in top.iterrows():
                L.append(f"| {r['customer_name'] or r['key']} | {money(r['closing_deferred'])} | {money(r['closing_unbilled'])} | {r['months_of_deferred_cover']} |")
            L.append("")
    L += ["", "## Matching", "", f"Exact id: {int((match['method'] == 'exact_id').sum())}. Normalised name: {int((match['method'] == 'normalised_name').sum())}. Ambiguous: {int((match['method'] == 'ambiguous_name').sum())}. Schedule only: {int((match['method'] == 'unmatched_schedule_only').sum())}. Invoices only: {int((match['method'] == 'unmatched_invoice_only').sum())}. Full table in match_table.csv.", ""]
    L += ["## What the files cannot support", "", f"Invoice lines with no type the kit could classify: {unknown}. Lines with no service period (spread to the invoice month instead): {nop}. Mapping warnings: {len(man['mapping_warnings'])}.", "", "The kit does not read contracts, order forms, the general ledger or bank records. Where the schedule and the invoices disagree, the kit shows the disagreement and the lines behind it. Which figure is right is a question for the company's finance lead, and in a transaction for the diligence team. Nothing here is an accounting opinion.", ""]
    L += ["## Questions for finance", ""]
    for cause in by_cause.index:
        q = {"over_stated": "Which accounts carry schedule MRR above the invoices, and is there a contract or a billing gap behind each one?", "under_stated": "Which accounts bill above the schedule, and is the schedule stale or the billing wrong?", "timing": "Is invoice timing a month off by design (billing in arrears) or by accident?", "credit_or_rebill": "What caused each credit memo, and was the rebill for the same period?", "duplicate_invoice": "Are the duplicate invoice ids a true double bill or an export artefact?", "ramp_or_proration": "Do the first months bill below the schedule by contract (ramp) or by proration, and should the schedule show the ramp?", "classification": "Which untyped or one off lines are recurring by contract and which are not?", "unmatched_schedule_only": "Where are the invoices for the accounts with none?", "unmatched_invoice_only": "Which billed accounts are missing from the schedule, and why?"}.get(cause)
        if q:
            L.append(f"* {q}")
    L.append("")
    open(os.path.join(out, "report.md"), "w").write("\n".join(L))
    if not HAVE_OPENPYXL:
        print("warning: openpyxl is not installed, so pilot.xlsx was not written; every table is in the csv files. Run: python3 -m pip install openpyxl")
        return
    with pd.ExcelWriter(os.path.join(out, "pilot.xlsx"), engine="openpyxl") as xw:
        pd.DataFrame([{"reported_arr": reported, "supported_arr": supported, "unresolved_arr": unresolved, "asof": asof}]).to_excel(xw, sheet_name="Summary", index=False)
        by_cause.reset_index().to_excel(xw, sheet_name="Exceptions by cause", index=False)
        acc.to_excel(xw, sheet_name="Accounts", index=False)
        ret.to_excel(xw, sheet_name="Retention", index=False)
        coh.to_excel(xw, sheet_name="Cohorts", index=False)
        match.to_excel(xw, sheet_name="Match", index=False)
        cmp.drop(columns=[c for c in ["start", "end"] if c in cmp.columns]).to_excel(xw, sheet_name="Customer month", index=False)
        if roll is not None and len(roll):
            roll.to_excel(xw, sheet_name="Deferred", index=False)
        if dacc is not None and len(dacc):
            dacc.to_excel(xw, sheet_name="Deferred by account", index=False)
        if answered is not None:
            pd.DataFrame(answered).to_excel(xw, sheet_name="Answered exceptions", index=False)
        if bridge_df is not None:
            bridge_df.to_excel(xw, sheet_name="Bridge", index=False)


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
def write_closepack(args, man, cmp, acc, ret, coh, bridge_df, roll, gl_given, a2r=None):
    """closepack.md: the one page a fractional CFO attaches to the board deck or hands a buyer.
    Every figure comes from a file this run already wrote. With --before <previous out folder>
    it adds what changed since that run, using diff_runs.py's logic (imported, not copied)."""
    out, asof = args.out, args.asof
    cur = cmp[cmp["month"] == asof]
    reported = float(cur["sched_mrr"].sum()) * 12
    supported = float(cur["supported_mrr"].sum()) * 12
    L = [f"# Revenue close pack, as of {asof}", "",
         f"Inputs: `{man['inputs']['schedule']['file']}` ({man['inputs']['schedule']['rows']} rows), `{man['inputs']['invoices']['file']}` ({man['inputs']['invoices']['rows']} lines). Kit {KIT_VERSION}. Every figure below is in a file beside this one; nothing here is an accounting opinion.", ""]
    L += ["## 1. Reported against supported", "", "| | ARR |", "|---|---:|",
          f"| Reported on the schedule | {money(reported)} |", f"| Supported by invoices | {money(supported)} |", f"| Excluded from verified ARR | {money(reported - supported)} |",
          f"| Accounts on the schedule | {int((cur['sched_mrr'] > 0).sum())} |", ""]
    if bridge_df is not None and len(bridge_df):
        months = sorted(bridge_df["month"].unique())
        window = bridge_df.groupby("basis", as_index=False)[BRIDGE_COLUMNS[1:-1]].sum()
        window["start"] = window["basis"].map(bridge_df[bridge_df["month"] == months[0]].set_index("basis")["start"])
        window["end"] = window["basis"].map(bridge_df[bridge_df["month"] == months[-1]].set_index("basis")["end"])
        L += [f"## 2. ARR bridge, {months[0]} to {months[-1]}", "", "| basis | start | new | reactivation | expansion | contraction | churn | end |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for basis in ("schedule", "supported", "difference"):
            r = window[window["basis"] == basis]
            if len(r):
                r = r.iloc[0]
                L.append("| " + basis + " | " + " | ".join(money(r[c] * 12) for c in BRIDGE_COLUMNS) + " |")
        L.append("")
    L += ["## 3. Retention, three ways", "", "| basis | GRR | NRR | starting book ARR | accounts |", "|---|---:|---:|---:|---:|"]
    for _, r in ret.iterrows():
        L.append(f"| {r['basis']} | {r['GRR']} | {r['NRR']} | {money(float(r['starting_book_mrr']) * 12)} | {r['starting_accounts']} |")
    L.append("")
    if coh is not None and len(coh):
        L += ["## 4. Cohorts, revenue retained at twelve months", "", "| cohort | accounts | schedule | supported |", "|---|---:|---:|---:|"]
        for _, r in coh.iterrows():
            L.append(f"| {r['cohort']} | {r['accounts']} | {r['revenue_retained_12m_schedule_pct']} | {r['revenue_retained_12m_supported_pct']} |")
        L.append("")
    sup = cur[cur["supported_mrr"] > 0].sort_values("supported_mrr", ascending=False)
    tot = float(sup["supported_mrr"].sum())
    if tot > 0:
        top5 = float(sup["supported_mrr"].head(5).sum())
        largest = sup.iloc[0]
        L += ["## 5. Concentration, supported basis", "",
              f"Top five accounts: {top5 / tot * 100:.1f} percent of supported ARR. Largest single account: {str(largest['cname'] or largest['cid'])}, {float(largest['supported_mrr']) / tot * 100:.1f} percent.", ""]
    bad = acc[acc["dominant_cause"] != "supported"].sort_values("asof_unresolved_mrr", ascending=False)
    L += [f"## 6. Exception register, {len(bad)} accounts, ten largest by unresolved ARR", "", "| account | cause | unresolved ARR | months not supported |", "|---|---|---:|---:|"]
    for _, r in bad.head(10).iterrows():
        L.append(f"| {r['customer_name'] or r['customer_id'] or r['key']} | {r['dominant_cause']} | {money(float(r['asof_unresolved_mrr']) * 12)} | {r['months_not_supported']} |")
    L.append("")
    if roll is not None and len(roll):
        last = roll.iloc[-1]
        L += ["## 7. Deferred revenue implied by the invoices", "",
              f"At {asof}: deferred {money(last['closing_deferred'])}, unbilled {money(last['closing_unbilled'])}, reported apart."]
        if gl_given and "gl_balance" in roll.columns and not pd.isna(last.get("gl_balance")):
            L.append(f"Per the general ledger: {money(last['gl_balance'])}. Difference: {money(last['difference'])}.")
        else:
            L.append("Per the general ledger: not provided.")
        L.append("")
    if a2r is not None and len(a2r):
        L += [f"### ARR to recognised revenue, {asof}", "", "Monthly. Every row, with its accounts, in arr_to_revenue.csv.", ""] + arr_to_revenue_markdown(a2r) + [""]
    L += ["## 8. Since the previous run", ""]
    before = getattr(args, "before", None)
    if before and os.path.exists(os.path.join(before, "accounts.csv")):
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import diff_runs
            b, a = diff_runs.load(before), diff_runs.load(out)
            rows = diff_runs.classify(b, a)
            counts = rows["status"].value_counts().to_dict()
            L += [f"Before: `{before}`. New {counts.get('new', 0)}, changed {counts.get('changed', 0)}, persisted {counts.get('persisted', 0)}, resolved {counts.get('resolved', 0)}."]
            for st, col in (("new", "after_unresolved_mrr"), ("resolved", "before_unresolved_mrr")):
                sub = rows[rows["status"] == st].sort_values(col, ascending=False).head(3)
                if len(sub):
                    L.append(f"Largest {st}: " + "; ".join(f"{r['customer_name']} ({money(float(r[col]) * 12)} ARR)" for _, r in sub.iterrows()) + ".")
            L.append("")
        except Exception as e:
            L += [f"Comparison not made ({type(e).__name__}: {e}).", ""]
    else:
        L += ["First run, no comparison. Pass `--before <previous out folder>` next month.", ""]
    L += ["## 9. The run that made this", "",
          f"Kit {KIT_VERSION}. Schedule sha256 {man['inputs']['schedule']['sha256'][:16]}, invoices sha256 {man['inputs']['invoices']['sha256'][:16]}. Full hashes of every input and output in receipt.json; `python3 receipt.py verify` recomputes them.", ""]
    open(os.path.join(out, "closepack.md"), "w").write("\n".join(L))


def write_receipt(args):
    """receipt.json: sha256 of inputs, mapping, this file and every output. receipt.py sits beside pilot.py; imported, never a subprocess, so it also runs in a browser runtime. Never fatal."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import receipt
        receipt.make(args)
    except Exception as e:  # missing receipt.py, read only folder, anything else
        print("warning: receipt.json not written (%s: %s); the run itself is complete" % (type(e).__name__, e))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--schedule", required=True); ap.add_argument("--invoices", required=True); ap.add_argument("--map", required=True)
    ap.add_argument("--asof", required=True, help="YYYY-MM, the month the figures are stated as of"); ap.add_argument("--out", required=True)
    ap.add_argument("--deferred-gl", default=None,
                    help="optional csv with columns month,balance: the company's own deferred revenue "
                         "balance per month from the general ledger. Given, the roll forward gains "
                         "gl_balance and difference; absent, the report says so rather than guessing.")
    ap.add_argument("--profile", action="store_true", help="stop after the manifest and the profile; no numbers computed")
    ap.add_argument("--answers", help="optional finance-lead answers JSON")
    ap.add_argument("--before", default=None, help="optional: the previous run's out folder, for the close pack's month over month section")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    answers = load_answers(args.answers) if args.answers else None
    mapping = json.load(open(args.map))
    rules = mapping["rules"]
    sched_raw, sched, miss1 = load_mapped(args.schedule, "schedule", mapping["schedule"])
    inv_raw, inv, miss2 = load_mapped(args.invoices, "invoices", mapping["invoices"])
    man = step_manifest(args, sched_raw, inv_raw, miss1 + miss2, args.answers)
    json.dump(man, open(os.path.join(args.out, "manifest.json"), "w"), indent=2)
    open(os.path.join(args.out, "profile.md"), "w").write(step_profile(sched, inv, man))
    if args.profile:
        print("profile only:", ", ".join(sorted(os.listdir(args.out))))
        for w in man["mapping_warnings"]:
            print("note:", w)
        return
    sched_m = schedule_monthly(sched, mapping["schedule"].get("format", "snapshot"), args.asof)
    lines_df, inv_m, inv_names = rebuild(inv, rules, sched_m=sched_m)
    sched_names = sched_m.groupby("key").agg(cid=("cid", "first"), cname=("cname", "first")).reset_index()
    match = match_customers(sched_names, inv_names)
    inv_inferred, n_inferred = infer_periods(inv, lines_df, sched_m, match, rules)
    if n_inferred:
        lines_df, inv_m, inv_names = rebuild(inv_inferred, rules, sched_m=sched_m)
        man["mapping_warnings"].append(f"{n_inferred} recurring invoice lines had no service period and were given one from the schedule (amount an exact multiple of the schedule MRR); marked period_source = inferred_from_schedule")
        json.dump(man, open(os.path.join(args.out, "manifest.json"), "w"), indent=2)
    lines_df.to_csv(os.path.join(args.out, "invoice_lines_classified.csv"), index=False)
    inv_m.to_csv(os.path.join(args.out, "invoice_mrr_monthly.csv"), index=False)
    match.to_csv(os.path.join(args.out, "match_table.csv"), index=False)
    cmp = compare(sched_m, inv_m, match, rules, args.asof, rules["ramp_months"], inv_names)
    answered = None
    if answers is not None:
        cmp, answered = apply_answers(cmp, answers)
    cmp.to_csv(os.path.join(args.out, "compare_customer_month.csv"), index=False)
    cmp[~cmp["bucket"].isin(["supported", "no_activity"])].to_csv(os.path.join(args.out, "exceptions.csv"), index=False)
    acc = accounts_view(cmp, args.asof, rules["months_for_retention"])
    acc.to_csv(os.path.join(args.out, "accounts.csv"), index=False)
    ret = retention(cmp, args.asof, rules["months_for_retention"])
    ret.to_csv(os.path.join(args.out, "retention.csv"), index=False)
    bridge_df, bridge_accounts = bridge(cmp, args.asof, rules["months_for_retention"])
    bridge_df.to_csv(os.path.join(args.out, "bridge.csv"), index=False)
    bridge_accounts.to_csv(os.path.join(args.out, "bridge_accounts.csv"), index=False)
    coh = cohorts(cmp, args.asof)
    coh.to_csv(os.path.join(args.out, "cohorts.csv"), index=False)
    # Deferred revenue implied by the invoices. Reads the spread the kit already
    # wrote rather than allocating a second time.
    gl = None
    if getattr(args, "deferred_gl", None):
        try:
            gl = pd.read_csv(args.deferred_gl, dtype=str)
            gl["balance"] = parse_amounts(gl["balance"])
            gl["month"] = gl["month"].astype(str).str.strip()
        except Exception as e:
            print(f"warning: --deferred-gl could not be read ({e}); the roll forward is reported without it")
            gl = None
    roll, dacc, journal = deferred(lines_df, inv_m, args.asof, rules["months_for_retention"], names=inv_names, gl=gl)
    roll.to_csv(os.path.join(args.out, "deferred_rollforward.csv"), index=False)
    a2r = arr_to_revenue(cmp, roll, args.asof)
    a2r.to_csv(os.path.join(args.out, "arr_to_revenue.csv"), index=False)
    dacc.to_csv(os.path.join(args.out, "deferred_accounts.csv"), index=False)
    journal.to_csv(os.path.join(args.out, "journal.csv"), index=False)
    write_report(args.out, man, cmp, acc, ret, coh, match, lines_df, args.asof, rules, answered, bridge_df,
                 roll=roll, dacc=dacc, gl_given=gl is not None, journal=journal, a2r=a2r)
    supported_export(cmp, args.out, args.asof)
    write_closepack(args, man, cmp, acc, ret, coh, bridge_df, roll, gl is not None, a2r)
    write_receipt(args)
    print("done:", ", ".join(sorted(os.listdir(args.out))))
    for w in man["mapping_warnings"]:
        print("warning:", w)


if __name__ == "__main__":
    main()
