# Verify this synthetic retention report offline

This folder contains invented data only. It was generated from the committed
seed in `synthetic_seed.json`; it is not a client export and is not derived
from one.

No install or network connection is needed. From this folder, run:

```sh
python3 verify_report.py retention_report.manifest.json --csv synthetic_input.csv --report retention_report.json
```

You should see every check that ran pass. To regenerate exactly the same input
bytes from the committed seed, run `python3 generate_synthetic_input.py`, then
run the verification command again.

## Tamper demonstration

`tamper/retention_report.json` changes exactly one measured field,
`accounts_analysed`. Run:

```sh
python3 verify_report.py retention_report.manifest.json --csv synthetic_input.csv --report tamper/retention_report.json
```

It must fail and name `accounts_analysed`. The recorded output is in
`tamper/verification.txt`.

## Bound

This establishes that the report was not altered and that it belongs to the
named input. It does not establish that Indexxero's computation from input to
number is correct.
