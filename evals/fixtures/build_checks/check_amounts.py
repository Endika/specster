import pytest
from reports.amounts import parse_amount
from reports.model import Report
from reports.totals import sum_amounts


@pytest.mark.parametrize(
    ("text", "value"),
    [("12", 12.0), ("12.5", 12.5), ("1,234.50", 1234.5), (" 7 ", 7.0), ("(5)", -5.0)],
)
def test_every_written_form(text: str, value: float) -> None:
    assert parse_amount(text) == value


def test_the_reported_symptom() -> None:
    assert sum_amounts(Report([{"amount": "1,234.50"}, {"amount": "10"}])) == 1244.5
