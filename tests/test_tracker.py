"""Security and update-boundary checks using synthetic data only."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cryptography.exceptions import InvalidTag
from tracker import decrypt, encrypt, compare, recalculate, accounting_fingerprint


def sample():
    return {
        "date": "01.01.2020", "attn": ["Нет комментария", "Проблема"],
        "closed": ["Закрыто"], "acc": [],
        "clinics": [{"sheet": "Клиника", "acc_date": "31.12.2019", "cut": "01.01.2020",
                     "dir_total": 10.03, "totals": {},
                     "rows": [{"n": "Поставщик", "p": 10.03, "d": 0, "dv": 10.03,
                               "c": "", "s": "Нет комментария"}]}],
    }


class CryptoBoundaryTests(unittest.TestCase):
    def test_compatible_envelope_and_tamper_detection(self):
        data = {"message": "Пример", "amount": 12.34}
        envelope = encrypt(data, b"synthetic-test-secret")
        self.assertEqual(decrypt(envelope, b"synthetic-test-secret"), data)
        self.assertEqual(set(envelope), {"v", "iter", "salt", "iv", "ct"})
        with self.assertRaises(InvalidTag):
            decrypt(envelope, b"different-test-secret")

    def test_nonce_and_salt_are_not_reused(self):
        one = encrypt({"a": 1}, b"test")
        two = encrypt({"a": 1}, b"test")
        self.assertNotEqual(one["salt"], two["salt"])
        self.assertNotEqual(one["iv"], two["iv"])

    def test_compressed_internal_state(self):
        state = {"rows": ["Пример"] * 1000}
        envelope = encrypt(state, b"test", compress=True)
        self.assertEqual(envelope["compression"], "zlib")
        self.assertEqual(decrypt(envelope, b"test"), state)

    def test_status_refresh_preserves_accounting_and_records_comment_changes(self):
        old = sample()
        recalculate(old)
        new = copy.deepcopy(old)
        row = new["clinics"][0]["rows"][0]
        row.update(c="неверный документ", s="Проблема")
        recalculate(new)
        self.assertEqual(accounting_fingerprint(old), accounting_fingerprint(new))
        delta = compare(old, new)
        self.assertEqual(delta["n_items"], 1)
        self.assertEqual(delta["per"]["Клиника"]["comments"], 1)
        self.assertEqual(delta["per"]["Клиника"]["statuses"], 1)
        row["p"] = 10.04
        self.assertNotEqual(accounting_fingerprint(old), accounting_fingerprint(new))

    def test_comment_only_edit_is_visible(self):
        old = sample()
        recalculate(old)
        new = copy.deepcopy(old)
        new["clinics"][0]["rows"][0]["c"] = "Дополнение"
        delta = compare(old, new)
        self.assertEqual(delta["n_items"], 1)
        self.assertEqual(delta["per"]["Клиника"]["statuses"], 0)


if __name__ == "__main__":
    unittest.main()
