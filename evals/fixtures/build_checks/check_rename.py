import warnings

import pytest
from reports.export import to_json
from reports.model import Report

ROWS = [{"name": "a", "amount": "1"}]


def test_new_name_with_no_warning() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert Report(ROWS).records == ROWS
        assert Report(records=ROWS).records == ROWS
        assert Report().records == []
        assert to_json(Report(records=ROWS)) == '[{"name": "a", "amount": "1"}]'


def test_deprecated_argument_and_attribute_still_work() -> None:
    with pytest.warns(DeprecationWarning):
        report = Report(rows=ROWS)
    assert report.records == ROWS
    with pytest.warns(DeprecationWarning):
        assert report.rows is report.records
