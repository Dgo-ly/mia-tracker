"""Safety checks for the automatic publication path."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from refresh_daily import automatic_patch


class AutomaticPatchTests(unittest.TestCase):
    def setUp(self):
        self.record = {"key": "ТЕСТ", "name": "Тест", "comments": ["запросили"],
                       "row": 2, "url": "https://example.invalid"}
        self.baseline = {
            "clinics": [{"sheet": "Клиника", "url": "https://example.invalid/doc/edit",
                         "rows": [{"n": "Тест", "p": 100, "c": self.record["comments"][0],
                                   "s": "Запрошено", "tabs": "Лист1"}]}],
            "statuses": [["Запрошено"], ["Проблема"]],
        }
        self.previous = {"sources": {"msk": {"file": {"id": "doc"},
                         "tabs": {"Лист1": {"records": [self.record]}}}}}
        self.current = copy.deepcopy(self.previous)
        new = self.current["sources"]["msk"]["tabs"]["Лист1"]["records"][0]
        new["comments"] = ["неверный документ"]
        self.comparison = {"structure_changes": [], "changes": [
            {"source": "msk", "supplier_key": "ТЕСТ", "tab": "Лист1", "requires_review": False,
             "before": [self.record], "after": [new], "changed_fields": ["comments"]},
        ]}

    def plan(self):
        return automatic_patch(self.baseline, self.previous, self.current, self.comparison)

    def test_exact_and_previously_verified_comment_gets_a_patch(self):
        updates, unresolved = self.plan()
        self.assertEqual(unresolved, 0)
        self.assertEqual(updates[0]["set"], {"c": "неверный документ", "s": "Проблема"})

    def test_duplicate_evidence_requires_review(self):
        self.previous["sources"]["msk"]["tabs"]["Лист2"] = {"records": [self.record]}
        self.assertEqual(self.plan(), ([], 1))

    def test_other_tab_with_distinct_comment_does_not_hide_exact_evidence(self):
        other = dict(self.record, comments=["другой комментарий"])
        for snapshot in (self.previous, self.current):
            snapshot["sources"]["msk"]["tabs"]["Лист2"] = {"records": [other]}
        updates, unresolved = self.plan()
        self.assertEqual(len(updates), 1)
        self.assertEqual(unresolved, 0)

    def test_deleted_comment_and_financial_changes_require_review(self):
        self.comparison["changes"][0]["after"][0]["comments"] = []
        self.assertEqual(self.plan(), ([], 1))
        self.comparison["changes"][0]["after"][0]["comments"] = ["неверный документ"]
        self.comparison["changes"][0]["changed_fields"] = ["comments", "p"]
        self.assertEqual(self.plan(), ([], 1))

    def test_unresolved_items_remain_part_of_the_gate(self):
        self.previous["pending_changes"] = [{"example": "unresolved"}]
        updates, unresolved = self.plan()
        self.assertEqual(len(updates), 1)
        self.assertEqual(unresolved, 1)


if __name__ == "__main__":
    unittest.main()
