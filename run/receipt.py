#!/usr/bin/env python3
"""The run receipt. One small file a stranger can recompute.

    python3 receipt.py make   --out out --schedule schedule.csv --invoices invoices.csv --map mapping.json
    python3 receipt.py verify --out out --schedule schedule.csv --invoices invoices.csv --map mapping.json

make   writes out/receipt.json: the sha256 of each input, of the mapping, of pilot.py itself,
       the kit version, the parameters, and the sha256 of every output file.
verify recomputes every hash and prints ok or MISMATCH per entry, then RECEIPT OK or RECEIPT FAILED.

It proves that these exact files, run by this exact code, produced these exact outputs.
It does not prove that the method is right or that the inputs are true.
Standard library only. No network.
"""
import argparse, hashlib, json, os, re, sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def entry(path):
    return {"file": os.path.basename(path), "bytes": os.path.getsize(path), "sha256": sha256(path)}


def kit_version(pilot_path):
    try:
        src = open(pilot_path, encoding="utf-8").read()
        m = re.search(r'^KIT_VERSION\s*=\s*"([^"]+)"', src, re.M)
        return m.group(1) if m else "unknown"
    except OSError:
        return "unknown"


def parameters(out, map_path):
    p = {}
    try:
        man = json.load(open(os.path.join(out, "manifest.json")))
        p["asof_month"] = man.get("asof_month")
    except (OSError, ValueError):
        p["asof_month"] = None
    try:
        rules = json.load(open(map_path)).get("rules", {})
        for k in ("tolerance_pct", "tolerance_abs", "ramp_months", "months_for_retention"):
            p[k] = rules.get(k)
    except (OSError, ValueError):
        pass
    return p


def optional_inputs(args, inputs):
    """The ledger balance file and the answers file change outputs, so when a run used them
    they are inputs and the receipt names them."""
    for role, attr in (("deferred_gl", "deferred_gl"), ("answers", "answers")):
        path = getattr(args, attr, None)
        if path and os.path.exists(path):
            inputs[role] = entry(path)
    return inputs


def build(args):
    pilot = os.path.join(HERE, "pilot.py")
    outputs = sorted(f for f in os.listdir(args.out)
                     if os.path.isfile(os.path.join(args.out, f)) and f != "receipt.json")
    return {
        "inputs": optional_inputs(args, {
            "schedule": entry(args.schedule),
            "invoices": entry(args.invoices),
            "mapping": entry(args.map),
        }),
        "kit_version": kit_version(pilot),
        "made_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "outputs": [entry(os.path.join(args.out, f)) for f in outputs],
        "parameters": parameters(args.out, args.map),
        "pilot_sha256": sha256(pilot) if os.path.exists(pilot) else None,
    }


def make(args):
    r = build(args)
    path = os.path.join(args.out, "receipt.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(r, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"receipt written: {path} ({len(r['outputs'])} outputs, kit {r['kit_version']})")
    return 0


def verify(args):
    path = os.path.join(args.out, "receipt.json")
    try:
        old = json.load(open(path))
    except (OSError, ValueError) as e:
        print(f"no readable receipt at {path}: {e}")
        print("RECEIPT FAILED")
        return 1
    new = build(args)
    failed = 0

    def check(label, a, b):
        nonlocal failed
        if a == b:
            print(f"ok        {label}")
        else:
            failed += 1
            print(f"MISMATCH  {label}  expected {a}  actual {b}")

    for k in sorted(set(old["inputs"]) | set(new["inputs"])):
        check(f"input {k}", old["inputs"].get(k, {}).get("sha256", "missing"), new["inputs"].get(k, {}).get("sha256", "missing"))
    check("pilot.py", old.get("pilot_sha256"), new.get("pilot_sha256"))
    check("kit_version", old.get("kit_version"), new.get("kit_version"))
    old_out = {o["file"]: o["sha256"] for o in old.get("outputs", [])}
    new_out = {o["file"]: o["sha256"] for o in new["outputs"]}
    for f in sorted(set(old_out) | set(new_out)):
        check(f"output {f}", old_out.get(f, "missing"), new_out.get(f, "missing"))
    print("RECEIPT OK" if failed == 0 else f"RECEIPT FAILED ({failed} mismatch)")
    return 0 if failed == 0 else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["make", "verify"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--schedule", required=True)
    ap.add_argument("--invoices", required=True)
    ap.add_argument("--map", required=True)
    ap.add_argument("--deferred-gl", default=None, help="the ledger balance file, if the run used one")
    ap.add_argument("--answers", default=None, help="the answers file, if the run used one")
    args = ap.parse_args()
    sys.exit(make(args) if args.command == "make" else verify(args))


if __name__ == "__main__":
    main()
