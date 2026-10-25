import unittest
from datetime import date

import pandas as pd

from features import supplier
from invoice_state import invoice_balances


class InvoiceBalanceTests(unittest.TestCase):
    def setUp(self):
        self.invoices = pd.DataFrame([
            dict(invoice_no=name, invoice_date="2026-01-01", total=1000,
                 supplier="Sonoma Dairy Supply", location="CHS", terms_days=14)
            for name in ["unpaid", "split", "full", "over", "duplicate"]
        ])
        self.payments = pd.DataFrame([
            ("split", "2026-01-10", 400), ("split", "2026-01-20", 600),
            ("full", "2026-01-10", 1000), ("over", "2026-01-10", 1100),
            ("duplicate", "2026-01-10", 1000), ("duplicate", "2026-01-11", 1000),
        ], columns=["invoice_no", "payment_date", "amount"])

    def test_payment_states_and_nonnegative_balances(self):
        balances = invoice_balances(self.invoices, self.payments, date(2026, 1, 15)).set_index("invoice_no")
        self.assertEqual(balances.outstanding.to_dict(),
                         {"unpaid": 1000, "split": 600, "full": 0, "over": 0, "duplicate": 0})
        self.assertEqual(balances.status.to_dict(),
                         {"unpaid": "pending", "split": "pending", "full": "settled",
                          "over": "settled", "duplicate": "settled"})

    def test_installments_and_inclusive_cutoff(self):
        for cutoff, outstanding in [("2026-01-09", 1000), ("2026-01-10", 600), ("2026-01-20", 0)]:
            with self.subTest(cutoff=cutoff):
                result = invoice_balances(self.invoices, self.payments, cutoff).set_index("invoice_no")
                self.assertEqual(result.loc["split", "outstanding"], outstanding)

    def test_unreceived_invoices_and_empty_payments(self):
        self.assertTrue(invoice_balances(self.invoices, self.payments, "2025-12-31").empty)
        result = invoice_balances(self.invoices, self.payments.iloc[:0], "2026-01-15")
        self.assertTrue((result.status == "pending").all())
        self.assertTrue((result.outstanding == 1000).all())

    def test_payments_before_bank_window_remain_settled(self):
        invoices = self.invoices.iloc[:1].assign(invoice_date=date(2025, 9, 15))
        payments = pd.DataFrame([dict(invoice_no="unpaid", payment_date=date(2025, 10, 1), amount=1000)])
        result = invoice_balances(invoices, payments, date(2025, 10, 25))
        self.assertEqual(result.status.iloc[0], "settled")
        self.assertEqual(result.outstanding.iloc[0], 0)

    def test_historical_status_overrides_snapshot_without_mutating_inputs(self):
        self.invoices["status"] = "settled"
        before = self.invoices.copy(deep=True)
        payments_before = self.payments.copy(deep=True)
        result = invoice_balances(self.invoices, self.payments, "2026-01-09")
        self.assertTrue((result.status == "pending").all())
        pd.testing.assert_frame_equal(self.invoices, before)
        pd.testing.assert_frame_equal(self.payments, payments_before)

    def test_supplier_features_use_historical_remaining_balance(self):
        self.invoices["invoice_date"] = pd.to_datetime(self.invoices.invoice_date)
        tables = {"invoices": self.invoices, "invoice_payments": self.payments}
        for cutoff, outstanding in [(date(2026, 1, 9), 5000), (date(2026, 1, 15), 1600),
                                    (date(2026, 1, 20), 1000)]:
            with self.subTest(cutoff=cutoff):
                self.assertEqual(supplier(tables, cutoff).open_ap.iloc[0], outstanding)


if __name__ == "__main__":
    unittest.main()
