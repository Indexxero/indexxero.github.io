#!/usr/bin/env python3
"""Check an Indexxero report yourself, on your machine, with our servers off.

    python3 verify_report.py REPORT.manifest.json
    python3 verify_report.py REPORT.manifest.json --csv the-export-you-sent.csv
    python3 verify_report.py REPORT.manifest.json --report REPORT.json

This file is the whole tool. It uses only the Python standard library, it
makes no network call, and it reads nothing except the files you name on the
command line. Python 3.8 or newer. Nothing to install.

WHAT EACH CHECK ANSWERS

    content hash     is this the same measurement? The manifest carries the
                     payload it hashed, so this recomputes the hash from the
                     numbers in front of you rather than taking ours.
    input            is this my data? Two values. The byte hash is exact and a
                     spreadsheet breaks it by re-saving. The row hash survives
                     a re-save and still moves on a real edit, so a byte
                     mismatch with a row match means the file was re-saved and
                     the data is unchanged.
    document seal    is this the same document? Only checkable if you were
                     given the report payload as JSON alongside the manifest.
    versions         which method and which build produced it. Stated so that
                     a different number in six months can be told apart from a
                     wrong number today.

WHAT THIS DOES NOT PROVE, and we would rather say it than have you find it

This establishes that the report was not altered and that it belongs to the
file you sent. It does NOT establish that our computation from your file to
the numbers is correct. Nothing you can run without our engine can establish
that. Any tool that claims otherwise is claiming more than it has.

A check we could not run prints NOT CHECKED and is never counted as a pass.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys

# Kept identical to the builder on purpose. Two correct runs must produce the
# same bytes, so the rules are written out rather than inferred.
FLOAT_PLACES = 6

PASS = "PASS"
FAIL = "FAIL"
NOT_CHECKED = "NOT CHECKED"
# Not a check. An explanation of two checks that disagreed, which is the
# common and harmless case. It is kept out of the count because a line that
# says PASS and is not a check inflates the denominator of our own verdict,
# and that is the class of defect this whole report exists to remove.
NOTE = "NOTE"


def serialize(payload):
    """One serialisation, fixed: sorted keys, no spare whitespace, utf-8."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def sha256_of(payload):
    return hashlib.sha256(serialize(payload)).hexdigest()


def _numeric_or_text(cell):
    """Compare numbers by value and everything else as text.

    A value with a leading zero before another digit stays text, because it is
    an identifier made of digits rather than a quantity. A spreadsheet that
    strips that zero HAS changed the value, and the row hash moves, which is
    correct and is stated in the report rather than normalised away.
    """
    stripped = cell.strip()
    if not stripped:
        return ""
    lowered = stripped.lower().lstrip("+-")
    if len(lowered) > 1 and lowered[0] == "0" and lowered[1].isdigit():
        return stripped
    try:
        value = float(stripped)
    except ValueError:
        return stripped
    if value != value or value in (float("inf"), float("-inf")):
        return stripped
    return repr(round(value, FLOAT_PLACES))


def canonical_rows(data):
    text = data.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows:
        return []
    header = [h.strip().lower() for h in rows[0]]
    order = sorted(range(len(header)), key=lambda i: (header[i], i))
    out = [[header[i] for i in order]]
    body = []
    for raw in rows[1:]:
        if not any(cell.strip() for cell in raw):
            continue
        padded = list(raw) + [""] * (len(header) - len(raw))
        body.append([_numeric_or_text(padded[i]) for i in order])
    body.sort(key=lambda r: "\x1f".join(r))
    out.extend(body)
    return out


def row_hash(data):
    rows = canonical_rows(data)
    return hashlib.sha256(
        "\x1e".join("\x1f".join(row) for row in rows).encode("utf-8")
    ).hexdigest()


def file_sha256(data):
    return hashlib.sha256(data).hexdigest()


def document_seal(report):
    """Over the whole document except the seal field, which cannot cover itself."""
    trimmed = {k: v for k, v in report.items() if k != "document_seal"}
    return sha256_of(json.loads(json.dumps(trimmed, sort_keys=True, default=str)))


def first_payload_difference(expected, actual, path=""):
    """Name the first manifest-payload value that differs in a report copy.

    The manifest payload is the independently hashable projection of the
    report. Looking only through that projection avoids calling a timestamp or
    other documented exclusion a tamper difference, while giving a reader a
    useful field name when a document-seal mismatch is real.
    """
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return path or "report"
        for key in sorted(expected):
            child = "{}.{}".format(path, key) if path else key
            if key not in actual:
                return child
            difference = first_payload_difference(expected[key], actual[key], child)
            if difference:
                return difference
        return None
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return path or "report"
        if len(expected) != len(actual):
            return "{} length".format(path)
        for index, (left, right) in enumerate(zip(expected, actual)):
            difference = first_payload_difference(
                left, right, "{}[{}]".format(path, index)
            )
            if difference:
                return difference
        return None
    return path if expected != actual else None


class Result:
    def __init__(self, name, status, detail=""):
        self.name = name
        self.status = status
        self.detail = detail

    def line(self):
        return "  {:<12}  {:<30}  {}".format(self.status, self.name, self.detail).rstrip()


def check_content_hash(manifest):
    if "payload" not in manifest:
        return Result("content hash", FAIL,
                      "the manifest carries no payload, so there is nothing "
                      "to check the hash against")
    stated = manifest.get("content_hash")
    if not stated:
        return Result("content hash", FAIL, "the manifest states no content hash")
    try:
        computed = sha256_of(manifest["payload"])
    except ValueError as exc:
        return Result("content hash", FAIL, "the payload will not serialise: {}".format(exc))
    if computed == stated:
        return Result("content hash", PASS, stated)
    return Result("content hash", FAIL,
                  "stated {} and the payload in this file hashes to {}".format(
                      stated, computed))


def check_input(manifest, csv_bytes):
    stated = manifest.get("input_fingerprint") or {}
    if csv_bytes is None:
        return [Result("input file", NOT_CHECKED,
                       "no file was given, so pass --csv with the export you "
                       "sent us to check it")]
    if stated.get("measurement") != "measured" or not stated.get("file_sha256"):
        return [Result("input file", NOT_CHECKED,
                       "this report states that no input file was fingerprinted, "
                       "so there is nothing here to compare your file with")]
    byte_ok = file_sha256(csv_bytes) == stated.get("file_sha256")
    row_ok = row_hash(csv_bytes) == stated.get("row_hash")
    results = [Result("input bytes", PASS if byte_ok else FAIL,
                      stated.get("file_sha256") if byte_ok
                      else "this file hashes to {}".format(file_sha256(csv_bytes)))]
    results.append(Result("input rows", PASS if row_ok else FAIL,
                          stated.get("row_hash") if row_ok
                          else "this file's rows hash to {}".format(row_hash(csv_bytes))))
    if row_ok and not byte_ok:
        results.append(Result("what that means", NOTE,
                              "the bytes differ and the data does not. This "
                              "file was re-saved, most likely by a spreadsheet, "
                              "and no value in it changed."))
    return results


def check_seal(manifest, report):
    stated = manifest.get("document_seal")
    if report is None:
        return Result("document seal", NOT_CHECKED,
                      "no report payload was given, so pass --report to check it")
    if not stated:
        return Result("document seal", NOT_CHECKED,
                      "this manifest states no document seal")
    computed = document_seal(report)
    if computed == stated:
        return Result("document seal", PASS, stated)
    differing_field = first_payload_difference(manifest.get("payload", {}), report)
    field_detail = (
        "; manifest payload differs at {}".format(differing_field)
        if differing_field else ""
    )
    return Result("document seal", FAIL,
                  "stated {} and this report seals to {}{}".format(
                      stated, computed, field_detail))


def check_versions(manifest, expect_method):
    method = manifest.get("method_version")
    if not method:
        return Result("versions", FAIL, "no method version is stated")
    if expect_method and method != expect_method:
        return Result("versions", FAIL,
                      "expected method version {} and this manifest states "
                      "{}".format(expect_method, method))
    engine = manifest.get("engine_version")
    if isinstance(engine, dict):
        engine_text = engine.get("value") or "not recorded by this build"
    else:
        engine_text = engine or "not stated"
    return Result("versions", PASS,
                  "method {}, engine {}".format(method, engine_text))


def verify(manifest, csv_bytes=None, report=None, expect_method=None):
    results = [check_content_hash(manifest)]
    results.extend(check_input(manifest, csv_bytes))
    results.append(check_seal(manifest, report))
    results.append(check_versions(manifest, expect_method))
    return results


def _read_json(path, label):
    try:
        with open(path, "rb") as handle:
            return json.loads(handle.read().decode("utf-8-sig"))
    except FileNotFoundError:
        print("Could not open the {} at {}".format(label, path))
        raise SystemExit(2)
    except ValueError as exc:
        print("The {} at {} is not valid JSON: {}".format(label, path, exc))
        raise SystemExit(2)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check an Indexxero report on your own machine.")
    parser.add_argument("manifest", help="the .manifest.json published with the report")
    parser.add_argument("--csv", help="the export file you sent us")
    parser.add_argument("--report", help="the report payload as JSON, if you were given it")
    parser.add_argument("--expect-method-version",
                        help="fail unless the manifest states this method version")
    args = parser.parse_args(argv)

    manifest = _read_json(args.manifest, "manifest")
    report = _read_json(args.report, "report payload") if args.report else None
    csv_bytes = None
    if args.csv:
        try:
            with open(args.csv, "rb") as handle:
                csv_bytes = handle.read()
        except FileNotFoundError:
            print("Could not open the export file at {}".format(args.csv))
            raise SystemExit(2)

    results = verify(manifest, csv_bytes, report, args.expect_method_version)

    print("")
    print("Indexxero report check")
    print("")
    for result in results:
        print(result.line())
    print("")

    checks = [r for r in results if r.status != NOTE]
    failed = [r for r in checks if r.status == FAIL]
    skipped = [r for r in checks if r.status == NOT_CHECKED]
    if failed:
        print("RESULT: FAIL. {} of {} checks did not pass.".format(
            len(failed), len(checks)))
    else:
        print("RESULT: PASS. Every check that ran passed.")
    if skipped:
        print("Not checked: {}. A check that did not run is not a check that "
              "passed.".format(", ".join(r.name for r in skipped)))
    print("")
    if failed:
        moved_field = (
            first_payload_difference(manifest.get("payload", {}), report)
            if report is not None else None
        )
        if moved_field:
            print("This FAIL establishes that the report does NOT match its "
                  "manifest. The field that moved is {}.".format(moved_field))
        else:
            print("This FAIL establishes that at least one supplied file does "
                  "not match this manifest. Read the failed check above for "
                  "the mismatch.")
    else:
        print(manifest.get("bound") or
              "This establishes that the report was not altered and that it "
              "belongs to the named input. It does not establish that the "
              "computation from input to number is correct.")
    print("")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
