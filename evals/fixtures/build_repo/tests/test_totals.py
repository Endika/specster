from reports.model import Report
from reports.totals import sum_amounts


def test_sum_amounts_adds_plain_amounts() -> None:
    assert sum_amounts(Report([{"amount": "12"}, {"amount": "0.5"}, {"name": "x"}])) == 12.5
