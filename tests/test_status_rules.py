import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
"""Synthetic tests only; safe to commit alongside status_rules.py."""
import unittest
from status_rules import SourceFlags, classify_status, is_explicit_missing_document_list


class StatusRulesTests(unittest.TestCase):
    def test_negative_keyword_wins_over_signed_keyword(self):
        self.assertEqual(classify_status("не подписан").status, "Проблема")

    def test_stale_wins_over_positive_keyword(self):
        decision = classify_status("в эдо подписано", paid=120, previous_paid=100, from_previous_cut=True)
        self.assertEqual(decision.status, "Коммент. устарел — сумма выросла")

    def test_problem_wins_over_stale(self):
        decision = classify_status("в эдо нет доков", paid=120, previous_paid=100, from_previous_cut=True)
        self.assertEqual(decision.status, "Проблема")

    def test_stale_requires_a_comment_and_increase(self):
        self.assertEqual(classify_status("", paid=120, previous_paid=100, from_previous_cut=True).status, "Нет комментария")
        self.assertEqual(classify_status("подписано", paid=100, previous_paid=100, from_previous_cut=True).status, "Закрыто")
        self.assertEqual(classify_status("подписано", paid=120, previous_paid=100).status, "Закрыто")

    def test_department_keyword_preserves_boundary(self):
        self.assertEqual(classify_status("вопрос: бух по поставщику").status, "Передано в другой отдел")
        self.assertEqual(classify_status("контакт Буханова").status, "В работе")

    def test_source_remainder_beats_closed_word(self):
        decision = classify_status("Завершено", clinic_kind="ami", flags=SourceFlags(remainder=True))
        self.assertEqual(decision.status, "Переплата / аванс")

    def test_green_source_flags(self):
        self.assertEqual(classify_status("", clinic_kind="ami", flags=SourceFlags(green_advance_row=True)).status, "Сверка / закрывающие есть / аванс")
        self.assertEqual(classify_status("", clinic_kind="ami", flags=SourceFlags(green_advance_row=True, remainder=True)).status, "Переплата — расходуется")

    def test_signature_flags_and_source_scope(self):
        self.assertEqual(classify_status("необходимо подписать", clinic_kind="krd").status, "Документы есть в ЭДО — подписать")
        self.assertEqual(classify_status("необходимо подписать", clinic_kind="krd", flags=SourceFlags(all_upd_signed=True)).status, "Подписано в ЭДО")
        with self.assertRaises(ValueError):
            classify_status("", clinic_kind="krd", flags=SourceFlags(all_upd_signed=True, unsigned_upd=True))

    def test_money_priorities_and_one_ruble_boundary(self):
        self.assertEqual(classify_status("подписано", paid=0, debt=10).status, "Долг перед поставщиком")
        self.assertEqual(classify_status("", paid="0.99").status, "Копейки (<1 ₽)")
        self.assertEqual(classify_status("", paid=1).status, "Нет комментария")
        self.assertEqual(classify_status("", paid=0).status, "Нет комментария")
        with self.assertRaises(ValueError):
            classify_status("", paid=float("nan"))

    def test_missing_document_extension_is_opt_in(self):
        comment = "1. Документ № 100 от 01.01.2020 на сумму 10,00; - не нашла\r\n2. Документ №101 - не нашла"
        self.assertEqual(classify_status(comment).status, "В работе")
        decision = classify_status(comment, explicit_missing_documents=True)
        self.assertEqual(decision.status, "Проблема")
        self.assertTrue(decision.enhancement)

    def test_missing_document_extension_is_narrow(self):
        cases = (
            "не нашла",
            "номер телефона - не нашла",
            "Документ без номера - не нашла",
            "Документ № 100 - не нашла только за январь",
            "Документ № 100 - не нашла, потом нашла",
            "Документ № 100 - не нашла\nПозже нашли все документы",
            "раньше Документ № 100 - не нашла",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertFalse(is_explicit_missing_document_list(text))
        self.assertTrue(is_explicit_missing_document_list("Документ № 100 — не нашёл"))

    def test_missing_document_extension_preserves_legacy_partial(self):
        self.assertEqual(classify_status("не нашла только за январь", explicit_missing_documents=True).status, "Частично закрыто")


if __name__ == "__main__":
    unittest.main()
