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

## What a PASS means, and what it does not

This establishes that the report matches its manifest and that the manifest
belongs to the named input.

It does not establish who sealed it. Anyone holding this folder can change the
report and re-seal it, so compare `content_hash` above with the value Indexxero
published for this report.

It does not establish that the computation from input to number is correct.

For this bundle, the published value is at
`https://measure.indexxero.com/proved.html`. For your own report, compare with
the delivery note.
