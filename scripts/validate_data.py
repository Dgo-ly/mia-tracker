#!/usr/bin/env python3
"""Validate a decrypted tracker snapshot without changing it.

All monetary arithmetic uses integer kopecks. ``dv`` is a signed director
balance; director totals include its positive part only. The validator does
not infer whether closing documents exist or rebuild legacy ``n_same``.

The Python API returns monetary totals for private processing. The CLI prints
only structural results; never put decrypted snapshots or detailed reports in
a public repository or CI log.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path


def name_key(value: str) -> str:
    """Conservative name key; deliberately no fuzzy/suffix/punctuation merge."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")).strip()


def _kopecks(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("Expected a numeric monetary value")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Monetary value must be finite")
    try:
        decimal = Decimal(str(value))
        if not decimal.is_finite():
            raise ValueError("Monetary value must be finite")
        scaled = decimal * 100
        if scaled != scaled.to_integral_value():
            raise ValueError("Monetary value must have at most two decimal places")
        return int(scaled)
    except InvalidOperation as exc:
        raise ValueError("Invalid monetary value") from exc


def _rubles(kopecks: int) -> str:
    return format(Decimal(kopecks) / 100, ".2f")


class _Checks:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, code, path, message):
        self.errors.append({"code": code, "path": path, "message": message})

    def money(self, value, path, nonnegative=False):
        try:
            result = _kopecks(value)
        except ValueError as exc:
            self.error("invalid_money", path, str(exc))
            return None
        if nonnegative and result < 0:
            self.error("negative_accounting_balance", path, "Accounting balances must be nonnegative")
        return result

    def count(self, value, path):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            self.error("invalid_count", path, "Expected a nonnegative integer")
            return None
        return value

    def expected_money(self, container, field, expected, path):
        actual = self.money(container.get(field), f"{path}.{field}")
        if actual is not None and expected is not None and actual != expected:
            self.error("money_total_mismatch", f"{path}.{field}", "Stored amount does not equal its detail rows")

    def expected_count(self, container, field, expected, path):
        actual = self.count(container.get(field), f"{path}.{field}")
        if actual is not None and expected is not None and actual != expected:
            self.error("count_mismatch", f"{path}.{field}", "Stored count does not equal its detail rows")


def validate(data) -> dict:
    """Return errors (strings), error_details, counts and exact totals.

    Never mutate ``data``. Structured details are separate so callers can
    display ``'; '.join(report['errors'])`` without processing diagnostics.
    """
    check = _Checks()
    counts = {"clinics": 0, "organizations": 0, "rows": 0, "positive_p_rows": 0,
              "reconciliation_rows": 0}
    totals = None
    if not isinstance(data, dict):
        detail = {"code": "invalid_snapshot", "path": "$", "message": "Expected an object"}
        return {"ok": False, "errors": ["invalid_snapshot at $: Expected an object"], "error_details": [detail],
                "warnings": [], "counts": counts, "totals": None}
    for field in ("date", "generated"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            check.error("missing_metadata", field, "Expected a nonempty date label")

    statuses = set()
    declarations = data.get("statuses")
    if not isinstance(declarations, list) or not declarations:
        check.error("invalid_status_dictionary", "statuses", "Expected a nonempty status dictionary")
        declarations = []
    for i, declaration in enumerate(declarations):
        if not isinstance(declaration, list) or not declaration or not isinstance(declaration[0], str) or not declaration[0].strip():
            check.error("invalid_status_dictionary", f"statuses[{i}]", "Expected a named status entry")
            continue
        status = declaration[0]
        if status in statuses:
            check.error("duplicate_status", f"statuses[{i}]", "Status names must be unique")
        statuses.add(status)
    groups = {}
    for field in ("attn", "closed"):
        declared = data.get(field)
        if not isinstance(declared, list) or any(not isinstance(s, str) for s in declared):
            check.error("invalid_status_group", field, "Expected a list of status names")
            declared = []
        groups[field] = set(declared)
        if len(groups[field]) != len(declared):
            check.error("duplicate_status_group", field, "Status group has duplicate entries")
        if not groups[field].issubset(statuses):
            check.error("unknown_status", field, "Status group references an undeclared status")
    if groups["attn"] & groups["closed"]:
        check.error("overlapping_status_groups", "attn/closed", "Attention and closed groups must not overlap")

    clinics = data.get("clinics")
    if not isinstance(clinics, list) or not clinics:
        check.error("invalid_clinics", "clinics", "Expected at least one clinic")
        clinics = []
    indexed = {}
    clinic_name_keys = set()
    all_money_valid = True
    total_p = total_d = total_director_positive = 0
    for ci, clinic in enumerate(clinics):
        path = f"clinics[{ci}]"
        if not isinstance(clinic, dict):
            check.error("invalid_clinic", path, "Expected an object")
            all_money_valid = False
            continue
        sheet = clinic.get("sheet")
        if not isinstance(sheet, str) or not sheet.strip():
            check.error("invalid_clinic_name", f"{path}.sheet", "Expected a nonempty clinic name")
            sheet = f"__invalid_clinic_{ci}"
        key = name_key(sheet)
        if key in clinic_name_keys:
            check.error("duplicate_clinic", f"{path}.sheet", "Clinic names must be unique")
        clinic_name_keys.add(key)
        rows = clinic.get("rows")
        if not isinstance(rows, list):
            check.error("invalid_rows", f"{path}.rows", "Expected a list of rows")
            rows = []
            all_money_valid = False
        prepared = []
        names = set()
        money_valid = True
        by_status = defaultdict(lambda: [0, 0])
        for ri, row in enumerate(rows):
            rp = f"{path}.rows[{ri}]"
            counts["rows"] += 1
            if not isinstance(row, dict):
                check.error("invalid_row", rp, "Expected an object")
                money_valid = False
                continue
            name = row.get("n")
            if not isinstance(name, str) or not name.strip():
                check.error("invalid_supplier_name", f"{rp}.n", "Expected a nonempty supplier name")
                name = f"__invalid_row_{ri}"
            nk = name_key(name)
            if nk in names:
                check.error("duplicate_supplier", f"{rp}.n", "Supplier name occurs more than once in this clinic")
            names.add(nk)
            status = row.get("s")
            if not isinstance(status, str) or status not in statuses:
                check.error("unknown_status", f"{rp}.s", "Row references an undeclared status")
                status = "__invalid_status"
            monetary = {field: check.money(row.get(field), f"{rp}.{field}", field in ("p", "d"))
                        for field in ("p", "d", "dv")}
            if any(v is None for v in monetary.values()):
                money_valid = False
                continue
            item = {"name_key": nk, "sheet": sheet, "path": rp, "status": status, **monetary}
            prepared.append(item)
            by_status[status][0] += 1
            by_status[status][1] += item["p"]

        stored = clinic.get("totals")
        if not isinstance(stored, dict):
            check.error("missing_totals", f"{path}.totals", "Expected clinic totals")
            stored = {}
        p = sum(r["p"] for r in prepared) if money_valid else None
        debt = sum(r["d"] for r in prepared) if money_valid else None
        positive_dv = sum(max(r["dv"], 0) for r in prepared) if money_valid else None
        positive_count = sum(r["p"] > 0 for r in prepared) if money_valid else None
        check.expected_money(stored, "unclosed", p, f"{path}.totals")
        check.expected_money(stored, "debt", debt, f"{path}.totals")
        check.expected_money(stored, "attention", sum(r["p"] for r in prepared if r["status"] in groups["attn"]) if money_valid else None, f"{path}.totals")
        check.expected_money(stored, "closed", sum(r["p"] for r in prepared if r["status"] in groups["closed"]) if money_valid else None, f"{path}.totals")
        check.expected_count(stored, "n", positive_count, f"{path}.totals")
        check.expected_money(clinic, "dir_total", positive_dv, path)
        stored_statuses = clinic.get("by_status")
        if not isinstance(stored_statuses, dict):
            check.error("missing_status_totals", f"{path}.by_status", "Expected per-status counts and amounts")
            stored_statuses = {}
        if money_valid and set(stored_statuses) != set(by_status):
            check.error("status_totals_membership", f"{path}.by_status", "Status totals must cover exactly the statuses used by rows")
        for status, values in stored_statuses.items():
            sp = f"{path}.by_status[{status!r}]"
            if status not in statuses:
                check.error("unknown_status", sp, "Totals reference an undeclared status")
            if not isinstance(values, list) or len(values) != 2:
                check.error("invalid_status_totals", sp, "Expected [row count, amount]")
                continue
            expected = by_status.get(status, [0, 0]) if money_valid else [None, None]
            actual_count = check.count(values[0], f"{sp}[0]")
            actual_amount = check.money(values[1], f"{sp}[1]")
            if expected[0] is not None and actual_count is not None and actual_count != expected[0]:
                check.error("count_mismatch", f"{sp}[0]", "Per-status count differs from rows")
            if expected[1] is not None and actual_amount is not None and actual_amount != expected[1]:
                check.error("money_total_mismatch", f"{sp}[1]", "Per-status amount differs from rows")
        indexed[sheet] = {"rows": prepared, "valid": money_valid, "path": path}
        all_money_valid = all_money_valid and money_valid
        if money_valid:
            total_p += p
            total_d += debt
            total_director_positive += positive_dv
            counts["positive_p_rows"] += positive_count
    counts["clinics"] = len(clinics)

    accounting = data.get("acc")
    if not isinstance(accounting, list) or not accounting:
        check.error("missing_accounting", "acc", "Expected accounting reconciliation for all clinics")
        accounting = []
    assigned = Counter()
    organizations = set()
    for ai, org in enumerate(accounting):
        path = f"acc[{ai}]"
        if not isinstance(org, dict):
            check.error("invalid_organization", path, "Expected an object")
            continue
        name = org.get("org")
        if not isinstance(name, str) or not name.strip():
            check.error("invalid_organization_name", f"{path}.org", "Expected a nonempty organization name")
        else:
            key = name_key(name)
            if key in organizations:
                check.error("duplicate_organization", f"{path}.org", "Legal entity must be counted once")
            organizations.add(key)
        sheets = org.get("sheets")
        if not isinstance(sheets, list) or not sheets or any(not isinstance(s, str) for s in sheets):
            check.error("invalid_organization_clinics", f"{path}.sheets", "Expected a nonempty list of clinic names")
            sheets = []
        if len(sheets) != len(set(sheets)):
            check.error("duplicate_organization_clinic", f"{path}.sheets", "A clinic cannot be counted twice")
        org_rows = []
        org_valid = True
        for sheet in sheets:
            assigned[sheet] += 1
            if sheet not in indexed:
                check.error("unknown_clinic", f"{path}.sheets", "Reconciliation references an unknown clinic")
                org_valid = False
                continue
            org_rows.extend(indexed[sheet]["rows"])
            org_valid = org_valid and indexed[sheet]["valid"]
        by_name = defaultdict(list)
        for row in org_rows:
            by_name[row["name_key"]].append(row)
        if any(len(rows) > 1 for rows in by_name.values()):
            check.error("duplicate_legal_entity_supplier", f"{path}.sheets", "Supplier is allocated more than once within the legal entity")
        for field, expected in (
            ("total_1c", sum(r["p"] for r in org_rows)),
            ("debt_1c", sum(r["d"] for r in org_rows)),
            ("total_dir", sum(max(r["dv"], 0) for r in org_rows)),
        ):
            check.expected_money(org, field, expected if org_valid else None, path)
        check.expected_count(org, "n_1c", sum(r["p"] > 0 for r in org_rows) if org_valid else None, path)
        check.expected_count(org, "n_dir", sum(r["dv"] > 0 for r in org_rows) if org_valid else None, path)
        # Legacy counter needs original source membership/debt semantics.
        check.count(org.get("n_same"), f"{path}.n_same")
        discrepancies = org.get("rows")
        if not isinstance(discrepancies, list):
            check.error("missing_reconciliation_rows", f"{path}.rows", "Expected a list, including an empty list when no discrepancies exist")
            discrepancies = []
        discrepancy_names = set()
        for ri, discrepancy in enumerate(discrepancies):
            rp = f"{path}.rows[{ri}]"
            counts["reconciliation_rows"] += 1
            if not isinstance(discrepancy, dict):
                check.error("invalid_reconciliation_row", rp, "Expected an object")
                continue
            n = discrepancy.get("n")
            if not isinstance(n, str) or not n.strip():
                check.error("invalid_supplier_name", f"{rp}.n", "Expected a nonempty supplier name")
                continue
            nk = name_key(n)
            if nk in discrepancy_names:
                check.error("duplicate_reconciliation_supplier", f"{rp}.n", "Supplier discrepancy appears more than once")
            discrepancy_names.add(nk)
            matches = by_name.get(nk, [])
            if len(matches) != 1:
                check.error("reconciliation_match", rp, "Discrepancy must match exactly one clinic supplier")
            for af, cf in (("a", "p"), ("w", "d"), ("dv", "dv")):
                actual = check.money(discrepancy.get(af), f"{rp}.{af}", af in ("a", "w"))
                if len(matches) == 1 and actual is not None and actual != matches[0][cf]:
                    check.error("reconciliation_amount_mismatch", f"{rp}.{af}", "Discrepancy amount differs from clinic row")
            if discrepancy.get("cat") not in {"only1c", "onlydir", "diff"}:
                check.error("invalid_reconciliation_category", f"{rp}.cat", "Unknown reconciliation category")
            if len(matches) == 1:
                if discrepancy.get("sh") != matches[0]["sheet"]:
                    check.error("reconciliation_clinic_mismatch", f"{rp}.sh", "Discrepancy points to a different clinic")
                if discrepancy.get("s") != matches[0]["status"]:
                    check.error("reconciliation_status_mismatch", f"{rp}.s", "Discrepancy status differs from clinic row")
    for sheet, clinic in indexed.items():
        if assigned[sheet] != 1:
            check.error("accounting_coverage", clinic["path"], "Every clinic must belong to exactly one accounting legal entity")
    counts["organizations"] = len(accounting)
    if all_money_valid:
        totals = {"p": _rubles(total_p), "d": _rubles(total_d),
                  "director_positive": _rubles(total_director_positive),
                  "p_kopecks": total_p, "d_kopecks": total_d}
    return {"ok": not check.errors,
            "errors": [f"{e['code']} at {e['path']}: {e['message']}" for e in check.errors],
            "error_details": check.errors, "warnings": check.warnings,
            "counts": counts, "totals": totals}


def _financial_state(data):
    rows = {}
    clinic_dates = {}
    for clinic in data["clinics"]:
        ck = name_key(clinic["sheet"])
        if ck in clinic_dates:
            raise ValueError("Duplicate clinic in financial snapshot")
        clinic_dates[ck] = clinic.get("acc_date")
        for row in clinic["rows"]:
            key = (ck, name_key(row["n"]))
            if key in rows:
                raise ValueError("Duplicate supplier in financial snapshot")
            rows[key] = tuple(_kopecks(row[field]) for field in ("p", "d", "dv"))
    orgs = {}
    for org in data["acc"]:
        key = name_key(org["org"])
        if key in orgs:
            raise ValueError("Duplicate legal entity in financial snapshot")
        orgs[key] = {
            "date": org.get("date"),
            "sheets": sorted(name_key(s) for s in org["sheets"]),
            "total_1c": _kopecks(org["total_1c"]), "debt_1c": _kopecks(org["debt_1c"]),
            "total_dir": _kopecks(org["total_dir"]),
            "n_1c": org["n_1c"], "n_dir": org["n_dir"], "n_same": org["n_same"],
        }
    return {"rows": [[*key, *values] for key, values in sorted(rows.items())],
            "clinic_accounting_dates": sorted(clinic_dates.items()),
            "organizations": sorted(orgs.items())}


def financial_fingerprint(data) -> str:
    """Stable financial/reference fingerprint, independent of row order/status.

    Includes signed dv, city assignment, accounting dates and legacy n_same.
    A status-only refresh must leave this fingerprint unchanged. Invalid
    monetary values or duplicate keys raise ValueError.
    """
    serialized = json.dumps(_financial_state(data), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def compare_accounting(before, after) -> dict:
    """Verify no financial reference was changed during a status-only refresh."""
    try:
        old = _financial_state(before)
        new = _financial_state(after)
    except (ValueError, KeyError, TypeError) as exc:
        return {"ok": False, "errors": [{"code": "invalid_financial_snapshot", "message": str(exc)}]}
    old_rows = {tuple(r[:2]): tuple(r[2:]) for r in old["rows"]}
    new_rows = {tuple(r[:2]): tuple(r[2:]) for r in new["rows"]}
    added = len(new_rows.keys() - old_rows.keys())
    removed = len(old_rows.keys() - new_rows.keys())
    changed = sum(old_rows[k] != new_rows[k] for k in old_rows.keys() & new_rows.keys())
    errors = []
    if added or removed or changed:
        errors.append({"code": "financial_rows_changed", "message": "Supplier amounts or allocation changed",
                       "added": added, "removed": removed, "changed": changed})
    if old["clinic_accounting_dates"] != new["clinic_accounting_dates"]:
        errors.append({"code": "accounting_date_changed", "message": "Clinic accounting reference dates changed"})
    if old["organizations"] != new["organizations"]:
        errors.append({"code": "accounting_reference_changed", "message": "Legal-entity reference totals, dates, allocation or legacy counters changed"})
    return {"ok": not errors, "errors": errors,
            "counts": {"added": added, "removed": removed, "changed": changed}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--report", type=Path, help="Optional PRIVATE full JSON report; contains monetary totals")
    args = parser.parse_args()
    report = validate(json.loads(args.snapshot.read_text(encoding="utf-8")))
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Paths can include status labels, but monetary totals/supplier names are not
    # printed. Keep --report output private just like the decrypted input.
    print(json.dumps({key: report[key] for key in ("ok", "counts", "errors", "warnings")}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
