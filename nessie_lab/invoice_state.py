"""Invoice balances derived from recorded payments at a historical cutoff."""
import pandas as pd


def invoice_balances(invoices, payments, as_of):
    """Return received invoices with status and outstanding cents as of the cutoff.

    Payments after the cutoff are excluded; overpayments cannot create negative AP.
    Input frames are not modified.
    """
    cutoff = pd.Timestamp(as_of)
    result = invoices[pd.to_datetime(invoices.invoice_date) <= cutoff].copy()
    paid = payments[pd.to_datetime(payments.payment_date) <= cutoff].groupby("invoice_no").amount.sum()
    amounts = result.invoice_no.map(paid).fillna(0).astype("int64")
    result["outstanding"] = (result.total - amounts).clip(lower=0).astype("int64")
    result["status"] = result.outstanding.map(lambda amount: "pending" if amount > 0 else "settled")
    return result
