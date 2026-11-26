"""Behavioral checks using fictitious suppliers and independently set controls."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from validate_data import compare_accounting, financial_fingerprint, validate


def example():
    # City A has a historical negative director balance. It must not reduce
    # the positive director total, even though the current accounting balance
    # for that supplier is an advance. City B shares the same legal entity.
    return {
        "date": "01.01.2030", "generated": "01.01.2030 19:00",
        "statuses": [["Review"], ["Complete"]], "attn": ["Review"], "closed": ["Complete"],
        "clinics": [
            {"sheet": "City A", "acc_date": "31.12.2029", "dir_total": 10.10,
             "rows": [
                 {"n": "Supplier One", "p": 10.10, "d": 0, "dv": 10.10, "s": "Review"},
                 {"n": "Supplier Two", "p": 0.20, "d": 0, "dv": -3.45, "s": "Complete"},
             ],
             "totals": {"unclosed": 10.30, "debt": 0, "attention": 10.10, "closed": 0.20, "n": 2},
             "by_status": {"Review": [1, 10.10], "Complete": [1, 0.20]}},
            {"sheet": "City B", "acc_date": "31.12.2029", "dir_total": 0.30,
             "rows": [{"n": "Supplier Three", "p": 0.30, "d": 2.50, "dv": 0.30, "s": "Complete"}],
             "totals": {"unclosed": 0.30, "debt": 2.50, "attention": 0, "closed": 0.30, "n": 1},
             "by_status": {"Complete": [1, 0.30]}},
        ],
        "acc": [{"org": "Example Company", "date": "31.12.2029", "sheets": ["City A", "City B"],
                 "total_1c": 10.60, "debt_1c": 2.50, "total_dir": 10.40,
                 "n_1c": 3, "n_dir": 2, "n_same": 2,
                 "rows": [{"cat": "diff", "n": "Supplier Two", "a": 0.20, "w": 0, "dv": -3.45,
                           "sh": "City A", "s": "Complete", "c": "Historical signed balance"}]}],
    }


def make_data():
    """Reusable synthetic payload for cryptography and refresh integration tests."""
    return example()


class ValidationTests(unittest.TestCase):
    def test_two_cities_one_legal_entity_and_negative_director_balance(self):
        report = validate(example())
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["counts"]["organizations"], 1)
        self.assertEqual(report["totals"]["p"], "10.60")
        self.assertEqual(report["totals"]["d"], "2.50")
        self.assertEqual(report["totals"]["director_positive"], "10.40")

    def test_one_kopeck_total_error_is_detected(self):
        data = example()
        data["clinics"][0]["totals"]["unclosed"] = 10.31
        report = validate(data)
        self.assertFalse(report["ok"])
        self.assertTrue(any(e["path"] == "clinics[0].totals.unclosed" for e in report["error_details"]))

    def test_signed_sum_cannot_replace_positive_director_total(self):
        data = example()
        data["clinics"][0]["dir_total"] = 6.65
        self.assertFalse(validate(data)["ok"])

    def test_city_cannot_be_counted_twice_under_another_entity(self):
        data = example()
        duplicate = copy.deepcopy(data["acc"][0])
        duplicate["org"] = "Second Company"
        data["acc"].append(duplicate)
        codes = {e["code"] for e in validate(data)["error_details"]}
        self.assertIn("accounting_coverage", codes)

    def test_duplicate_supplier_across_city_allocation_is_rejected(self):
        data = example()
        data["clinics"][1]["rows"][0]["n"] = "  SUPPLIER   ONE "
        codes = {e["code"] for e in validate(data)["error_details"]}
        self.assertIn("duplicate_legal_entity_supplier", codes)

    def test_missing_debt_or_nan_cannot_be_treated_as_zero(self):
        for value in (None, float("nan"), float("inf"), True, "0"):
            with self.subTest(value=value):
                data = example()
                data["clinics"][0]["rows"][0]["d"] = value
                report = validate(data)
                self.assertFalse(report["ok"])
                self.assertIsNone(report["totals"])

    def test_unknown_status_and_missing_summary_are_rejected(self):
        data = example()
        data["clinics"][0]["rows"][0]["s"] = "Unlisted"
        del data["clinics"][1]["by_status"]
        codes = {e["code"] for e in validate(data)["error_details"]}
        self.assertIn("unknown_status", codes)
        self.assertIn("missing_status_totals", codes)

    def test_reconciliation_amount_cannot_drift_from_clinic(self):
        data = example()
        data["acc"][0]["rows"][0]["a"] = 0.21
        codes = {e["code"] for e in validate(data)["error_details"]}
        self.assertIn("reconciliation_amount_mismatch", codes)

    def test_status_refresh_and_row_reordering_preserve_fingerprint(self):
        before = example()
        after = copy.deepcopy(before)
        after["clinics"].reverse()
        after["clinics"][1]["rows"].reverse()
        after["clinics"][1]["rows"][0]["s"] = "Review"
        after["clinics"][1]["rows"][0]["c"] = "Updated comment"
        self.assertTrue(compare_accounting(before, after)["ok"])
        self.assertEqual(financial_fingerprint(before), financial_fingerprint(after))

    def test_row_level_financial_change_is_found_even_with_same_grand_total(self):
        before = example()
        after = copy.deepcopy(before)
        after["clinics"][0]["rows"][0]["p"] = 10.09
        after["clinics"][0]["rows"][1]["p"] = 0.21
        report = compare_accounting(before, after)
        self.assertFalse(report["ok"])
        self.assertEqual(report["counts"]["changed"], 2)

    def test_legacy_counter_and_accounting_date_are_preserved(self):
        before = example()
        after = copy.deepcopy(before)
        after["acc"][0]["n_same"] = 3
        self.assertFalse(compare_accounting(before, after)["ok"])
        after = copy.deepcopy(before)
        after["clinics"][0]["acc_date"] = "31.01.2030"
        self.assertFalse(compare_accounting(before, after)["ok"])

    def test_validation_does_not_modify_the_payload(self):
        data = example()
        untouched = copy.deepcopy(data)
        validate(data)
        self.assertEqual(data, untouched)


if __name__ == "__main__":
    unittest.main()
