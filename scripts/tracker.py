"""Prepare and encrypt reviewed tracker updates without publishing plaintext.

The web client's AES-GCM/PBKDF2 envelope is preserved. Accounting balances are
immutable in a daily refresh. A new accounting period needs a separate import.
No network or GitHub writes are performed by this module.
"""
from __future__ import annotations

import argparse
import base64
import copy
import getpass
import hashlib
import json
import os
import zlib
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_PATCH_FIELDS = {"c", "s", "src", "edo", "dep", "tabs", "x"}


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def cents(value):
    return int((Decimal(str(value or 0)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def rubles(value):
    return value / 100


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def save(path, value, *, plaintext=True):
    target = Path(path).resolve()
    if plaintext and target.is_relative_to(ROOT):
        raise ValueError("Plaintext output must be outside the public repository.")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    temporary.replace(target)


def password():
    value = os.environ.get("MIA_TRACKER_PASSWORD") or getpass.getpass("Tracker password: ")
    if not value:
        raise ValueError("A non-empty password is required.")
    return value.encode("utf-8")


def decrypt(envelope, secret):
    if envelope.get("v") != 1:
        raise ValueError("Unsupported encryption envelope version.")
    iterations = envelope["iter"]
    if not isinstance(iterations, int) or not 100_000 <= iterations <= 10_000_000:
        raise ValueError("Unexpected PBKDF2 work factor.")
    salt = base64.b64decode(envelope["salt"], validate=True)
    iv = base64.b64decode(envelope["iv"], validate=True)
    ciphertext = base64.b64decode(envelope["ct"], validate=True)
    if len(salt) < 16 or len(iv) != 12:
        raise ValueError("Unexpected salt/nonce length.")
    key = hashlib.pbkdf2_hmac("sha256", secret, salt, iterations, dklen=32)
    plaintext = AESGCM(key).decrypt(iv, ciphertext, None)
    compression = envelope.get("compression")
    if compression == "zlib":
        decoder = zlib.decompressobj()
        plaintext = decoder.decompress(plaintext, 20_000_000)
        if decoder.unconsumed_tail or not decoder.eof:
            raise ValueError("Compressed state exceeds the supported size.")
    elif compression is not None:
        raise ValueError("Unknown compression mode.")
    return json.loads(plaintext)


def encrypt(data, secret, *, compress=False):
    salt, iv = os.urandom(16), os.urandom(12)
    iterations = 250_000
    key = hashlib.pbkdf2_hmac("sha256", secret, salt, iterations, dklen=32)
    plaintext = canonical(data)
    if compress:
        plaintext = zlib.compress(plaintext, level=9)
    ciphertext = AESGCM(key).encrypt(iv, plaintext, None)
    envelope = {"v": 1, "iter": iterations, **{
        k: base64.b64encode(v).decode("ascii") for k, v in
        (("salt", salt), ("iv", iv), ("ct", ciphertext))}}
    if compress:
        envelope["compression"] = "zlib"
    if decrypt(envelope, secret) != data:
        raise ValueError("Encryption round-trip failed.")
    return envelope


def index(data):
    result = {}
    for clinic in data["clinics"]:
        for row in clinic["rows"]:
            key = (clinic["sheet"], row["n"])
            if key in result:
                raise ValueError("Duplicate clinic/supplier identity.")
            result[key] = row
    return result


def accounting_fingerprint(data):
    return digest({
        "rows": sorted((cl, name, cents(row["p"]), cents(row["d"]), cents(row.get("dv")))
                       for (cl, name), row in index(data).items()),
        "accounting": [{k: v for k, v in org.items() if k != "rows"} for org in data["acc"]],
        "cuts": [(c["sheet"], c.get("acc_date"), c.get("cut"), c.get("dir_total"))
                 for c in data["clinics"]],
    })


def recalculate(data):
    attention, closed = set(data["attn"]), set(data["closed"])
    for clinic in data["clinics"]:
        rows = clinic["rows"]
        by_status = defaultdict(lambda: [0, 0])
        for row in rows:
            aggregate = by_status[row["s"]]
            aggregate[0] += 1
            aggregate[1] += cents(row["p"])
        clinic["by_status"] = {status: [n, rubles(p)] for status, (n, p) in by_status.items()}
        clinic["totals"].update({
            "unclosed": rubles(sum(cents(r["p"]) for r in rows)),
            "debt": rubles(sum(cents(r["d"]) for r in rows)),
            "n": sum(cents(r["p"]) > 0 for r in rows),
            "attention": rubles(sum(cents(r["p"]) for r in rows if r["s"] in attention)),
            "closed": rubles(sum(cents(r["p"]) for r in rows if r["s"] in closed)),
        })
    row_index = index(data)
    for org in data["acc"]:
        for discrepancy in org["rows"]:
            key = (discrepancy["sh"], discrepancy["n"])
            if key not in row_index:
                raise ValueError("A reconciliation row has no matching supplier.")
            row = row_index[key]
            discrepancy["s"], discrepancy["c"] = row["s"], row["c"]


def compare(before, after):
    before_clinics = {c["sheet"]: c for c in before["clinics"]}
    per, items = {}, []
    for clinic in after["clinics"]:
        previous = before_clinics.get(clinic["sheet"])
        old = {r["n"]: r for r in previous["rows"]} if previous else {}
        now = {r["n"]: r for r in clinic["rows"]}
        comments = statuses = 0
        for name, row in now.items():
            original = old.get(name)
            if original is None:
                continue
            comment_changed = original["c"] != row["c"]
            status_changed = original["s"] != row["s"]
            comments += comment_changed
            statuses += status_changed
            if comment_changed or status_changed:
                items.append({"cl": clinic["sheet"], "n": name, "p": row["p"],
                              "f": original["s"], "t": row["s"], "c": row["c"], "cc": comment_changed})
        per[clinic["sheet"]] = {
            "comments": comments, "statuses": statuses,
            "att_before": previous["totals"]["attention"] if previous else None,
            "att_now": clinic["totals"]["attention"],
            "unclosed_before": previous["totals"]["unclosed"] if previous else None,
            "unclosed_now": clinic["totals"]["unclosed"],
            "gone": len(set(old) - set(now)), "known": previous is not None,
        }
    items.sort(key=lambda r: (-cents(r["p"]), r["cl"], r["n"]))
    return {"base": before["date"], "per": per, "n_items": len(items), "items": items}


def prepare(baseline, changes, week, *, generated=None):
    from validate_data import validate
    initial = validate(baseline)
    if initial["errors"]:
        raise ValueError("Baseline validation failed: " + "; ".join(initial["errors"]))
    if changes.get("baseline_sha256") != digest(baseline):
        raise ValueError("The reviewed patch belongs to a different baseline.")
    if changes.get("source_count") != len(baseline["clinics"]):
        raise ValueError("A refresh must account for every source clinic.")
    result = copy.deepcopy(baseline)
    rows = index(result)
    seen = set()
    for change in changes.get("updates", []):
        key = (change["clinic"], change["name"])
        if key in seen or key not in rows:
            raise ValueError("Duplicate or unmatched patch identity.")
        seen.add(key)
        row = rows[key]
        if not change.get("source") or not change.get("reason"):
            raise ValueError("Each patch needs source evidence and a reason.")
        if not set(change["set"]) <= ALLOWED_PATCH_FIELDS:
            raise ValueError("Daily patches cannot change accounting balances or identities.")
        if any(row.get(field) != value for field, value in change["before"].items()):
            raise ValueError("The supplier changed after the patch was reviewed.")
        row.update(change["set"])
    recalculate(result)
    now = generated or datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M")
    datetime.strptime(now, "%d.%m.%Y %H:%M")
    result["generated"], result["date"] = now, now[:10]
    result["day"], result["week"] = compare(baseline, result), compare(week, result)
    result["refresh"] = {
        "schema_version": 1, "method": "reviewed_director_changes",
        "accounting_reimported": False,
        "source_checked_at": changes["source_checked_at"],
        "source_count": changes["source_count"],
        "patch_sha256": digest(changes),
        "baseline_sha256": digest(baseline),
        "status_rules": changes.get("status_rules", "legacy-compatible"),
    }
    if accounting_fingerprint(result) != accounting_fingerprint(baseline):
        raise ValueError("Accounting data changed during a daily refresh.")
    final = validate(result)
    if final["errors"]:
        raise ValueError("Candidate validation failed: " + "; ".join(final["errors"]))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("decrypt", "encrypt", "validate"):
        p = sub.add_parser(name)
        p.add_argument("input")
        if name != "validate":
            p.add_argument("output")
        if name == "encrypt":
            p.add_argument("--compress-state", action="store_true",
                           help="Internal state only; never use for the browser's data.enc")
    p = sub.add_parser("prepare")
    p.add_argument("--baseline", required=True)
    p.add_argument("--patches", required=True)
    p.add_argument("--week", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--generated")
    args = parser.parse_args()
    if args.command == "decrypt":
        save(args.output, decrypt(load(args.input), password()))
    elif args.command == "encrypt":
        if args.compress_state and Path(args.output).name == "data.enc":
            raise ValueError("The browser payload must not be compressed.")
        save(args.output, encrypt(load(args.input), password(), compress=args.compress_state), plaintext=False)
    elif args.command == "validate":
        from validate_data import validate
        report = validate(load(args.input))
        print(json.dumps({k: report[k] for k in ("ok", "counts", "errors", "warnings")},
                         ensure_ascii=False, indent=2))
        if report["errors"]:
            raise SystemExit(1)
    else:
        data = prepare(load(args.baseline), load(args.patches), load(args.week), generated=args.generated)
        save(args.output, data)
        print(json.dumps({"generated": data["generated"], "day_changes": data["day"]["n_items"],
                          "week_changes": data["week"]["n_items"], "accounting_changed": False}))


if __name__ == "__main__":
    main()
