import csv
import io

from reports.export import to_csv
from reports.model import Report


def test_header_rows_missing_cell_and_non_ascii() -> None:
    out = to_csv(Report([{"name": "é", "amount": "1"}, {"amount": "2", "note": "x,y"}]))
    rows = list(csv.reader(io.StringIO(out)))
    assert rows == [["name", "amount", "note"], ["é", "1", ""], ["", "2", "x,y"]]
    assert out.endswith("\n") and "\r" not in out


def test_empty_report() -> None:
    assert to_csv(Report([])) == ""
