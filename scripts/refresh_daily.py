"""Prepare an automated refresh for unambiguous director comments."""
from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from director_sources import ACTIVE_TABS, compare_states
from status_rules import classify_status
from tracker import decrypt, digest, encrypt, index, load, prepare, save
from validate_data import compare_accounting, validate


TEXT_RULES = {
    "problem_keyword", "explicit_missing_document_list", "partial_keyword",
    "advance_keyword", "closed_keyword", "sent_keyword", "requested_keyword",
    "department_keyword", "comment_fallback",
}


def automatic_patch(baseline, previous, current, comparison):
    suppliers = index(baseline)
    clinics = {}
    current_names = {}
    old_names = {}
    for source, value in current["sources"].items():
        file_id = value["file"]["id"]
        matches = [clinic["sheet"] for clinic in baseline["clinics"]
                   if file_id in clinic["url"].split("/")]
        if len(matches) == 1:
            clinics[source] = matches[0]
        for state, output in ((previous, old_names), (current, current_names)):
            output[source] = Counter(
                record["key"] for tab in state["sources"][source]["tabs"].values()
                for record in tab["records"]
            )
    updates = []
    unresolved = len(comparison["structure_changes"]) + len(previous.get("pending_changes", []))
    allowed = {status[0] for status in baseline["statuses"]}
    for change in comparison["changes"]:
        source = change["source"]
        key = change["supplier_key"]
        if (source not in clinics
                or change["requires_review"] or change["changed_fields"] != ["comments"]
                or old_names[source][key] != 1 or current_names[source][key] != 1):
            unresolved += 1
            continue
        before, after = change["before"][0], change["after"][0]
        identity = (clinics[source], before["name"])
        row = suppliers.get(identity)
        if (not row or row["p"] < 1 or before["name"] != after["name"]
                or not before["comments"] or len(before["comments"]) != 1
                or row["c"] != before["comments"][0]
                or len(after["comments"]) != 1 or not after["comments"][0]
                or before.get("include_supplier_scope") is False
                or before.get("overpayment_flag")
                or change["tab"] not in row["tabs"]):
            unresolved += 1
            continue
        old_status = classify_status(row["c"], clinic_kind=source, explicit_missing_documents=True)
        new_status = classify_status(after["comments"][0], clinic_kind=source, explicit_missing_documents=True)
        if (old_status.status != row["s"] or old_status.rule not in TEXT_RULES
                or new_status.rule not in TEXT_RULES or new_status.status not in allowed):
            unresolved += 1
            continue
        updates.append({
            "clinic": identity[0], "name": identity[1],
            "before": {"c": row["c"], "s": row["s"]},
            "set": {"c": after["comments"][0], "s": new_status.status},
            "source": {"file_id": current["sources"][source]["file"]["id"],
                       "tab": change["tab"], "row": after["row"], "url": after["url"]},
            "reason": new_status.rule,
        })
    return updates, unresolved


def history(target, reference, secret):
    for revision in subprocess.check_output(
        ["git", "log", "--format=%H", "--first-parent", reference, "--", "data.enc"],
        text=True,
    ).splitlines():
        envelope = json.loads(subprocess.check_output(["git", "show", f"{revision}:data.enc"]))
        payload = decrypt(envelope, secret)
        if datetime.strptime(payload["date"], "%d.%m.%Y").date() <= target:
            return payload
    raise ValueError("No weekly comparison snapshot found")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    secret = os.environ.get("MIA_TRACKER_PASSWORD")
    if not secret:
        parser.exit(1, "MIA_TRACKER_PASSWORD is missing.\n")
    secret = secret.encode()
    try:
        baseline = decrypt(load("data.enc"), secret)
        previous = decrypt(load("source-state.enc"), secret)
        review = decrypt(load(args.review), secret)
        current = review["current"]
        if (digest(baseline) != previous["published_data_sha256"] or not validate(baseline)["ok"]
                or set(current["sources"]) != set(ACTIVE_TABS)
                or any(current["sources"][k]["file"]["id"] != previous["sources"][k]["file"]["id"]
                       for k in ACTIVE_TABS)):
            raise ValueError("Invalid published data or source identities")
        comparison = compare_states(previous, current)
        updates, unresolved = automatic_patch(baseline, previous, current, comparison)
        if unresolved:
            print(f"result=review_required updates={len(updates)} unresolved={unresolved}")
            return
        if not updates:
            print("result=unchanged updates=0")
            return

        now = datetime.now(ZoneInfo("Europe/Moscow"))
        if baseline["date"] == now.strftime("%d.%m.%Y"):
            print("result=review_required updates=0 unresolved=1")
            return
        week = history(now.date() - timedelta(days=7), args.reference, secret)
        patch = {
            "baseline_sha256": digest(baseline), "source_checked_at": current["captured_at"],
            "source_count": len(current["sources"]), "status_rules": previous["status_rules"],
            "updates": updates,
        }
        candidate = prepare(baseline, patch, week, generated=now.strftime("%d.%m.%Y %H:%M"))
        if not compare_accounting(baseline, candidate)["ok"]:
            raise ValueError("Accounting state changed")
        state = copy.deepcopy(previous)
        state.update(current)
        state.update(published_data_sha256=digest(candidate), published_generated=candidate["generated"],
                     last_reviewed_patch=patch)
        save(args.output / "data.enc", encrypt(candidate, secret), plaintext=False)
        save(args.output / "source-state.enc", encrypt(state, secret, compress=True), plaintext=False)
    except Exception as exc:
        parser.exit(1, f"Daily refresh failed ({type(exc).__name__}).\n")
    print(f"result=ready updates={len(updates)}")


if __name__ == "__main__":
    main()
