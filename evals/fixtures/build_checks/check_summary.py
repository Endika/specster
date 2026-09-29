from reports.export import summary
from reports.model import Report


def test_total_skips_rows_without_the_field() -> None:
    report = Report([{"amount": "1.5"}, {"name": "a"}, {"amount": "2"}])
    assert report.total("amount") == 3.5
    assert Report([]).total("amount") == 0.0


def test_summary() -> None:
    report = Report([{"amount": "1.5"}, {"amount": "2"}])
    assert summary(report) == "2 rows, total amount 3.50"
