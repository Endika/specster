from reports.csv_io import from_csv, to_csv
from reports.model import Report

TRICKY = [
    {"name": 'say "hi", then go', "amount": "1"},
    {"name": "two\nlines", "note": "é"},
    {"amount": ""},
]


def test_round_trip_of_tricky_values() -> None:
    back = from_csv(to_csv(Report(TRICKY)))
    union = ["name", "amount", "note"]
    assert back.rows == [{k: row.get(k, "") for k in union} for row in TRICKY]


def test_crlf_bom_and_empty_text() -> None:
    assert from_csv("﻿a,b\r\n1,\r\n").rows == [{"a": "1", "b": ""}]
    assert from_csv("").rows == []
    assert to_csv(Report([{"a": "1"}])) == "a\n1\n"
