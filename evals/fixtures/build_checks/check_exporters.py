from reports.markdown import to_markdown
from reports.model import Report
from reports.tsv import to_tsv

ROWS = [{"a": "x", "b": "y"}, {"a": "1", "b": "2"}]


def test_tsv() -> None:
    assert to_tsv(Report(ROWS)) == "a\tb\nx\ty\n1\t2\n"
    assert to_tsv(Report([])) == ""


def test_markdown() -> None:
    assert to_markdown(Report(ROWS)) == "| a | b |\n| --- | --- |\n| x | y |\n| 1 | 2 |"
    assert to_markdown(Report([])) == ""
