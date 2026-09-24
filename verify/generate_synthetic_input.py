#!/usr/bin/env python3
"""Regenerate synthetic_input.csv from the committed synthetic_seed.json."""
import csv
import json
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
seed = json.loads((HERE / "synthetic_seed.json").read_text())["seed"]
rng = random.Random(seed)
plans = ("Starter", "Growth", "Scale")
regions = ("North", "South", "East", "West")
with (HERE / "synthetic_input.csv").open("w", newline="", encoding="utf-8") as handle:
    writer = csv.DictWriter(handle, fieldnames=("account_id", "seats", "arr", "plan_tier", "region", "monthly_active_users"), lineterminator="\n")
    writer.writeheader()
    for number in range(1, 73):
        seats = rng.randint(3, 120)
        writer.writerow({
            "account_id": "SYN-{:03d}".format(number),
            "seats": seats,
            "arr": seats * rng.choice((900, 1200, 1500)),
            "plan_tier": rng.choice(plans),
            "region": rng.choice(regions),
            "monthly_active_users": rng.randint(seats, seats * 4),
        })
