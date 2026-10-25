"""The INTERIM rule. tools no longer uses this module: checks_bk applies the same rule ("downgrade" every finding)
while the owner has not decided, and the owner's accept / dismiss / revise once they have.

Under this rule no finding is verified, so every one is downgraded to the owner (status "downgrade"). Through
contracts.passed_adjustments that means only CAUTIOUS adjustments (money still owed, an overcharge that keeps costing)
reach the Forecaster, never as recoverable, and nothing that would make the forecast rosier (excluding or correcting
a bank row) is used."""
import contracts as K

REASON = "interim check: checks.py (the verifier) is not built yet, so the owner must confirm this finding"


def check_findings(findings) -> list:
    """-> one CheckResult(subject="bookkeeper", status="downgrade") per finding."""
    return [K.CheckResult(subject="bookkeeper", ref=f.finding_id, status="downgrade", reasons=[REASON]) for f in findings]


def passed(adjustments, findings) -> list:
    """The adjustments the Forecaster may use under the interim checks (CAUTIOUS kinds only, not recoverable)."""
    return K.passed_adjustments(adjustments, check_findings(findings))
