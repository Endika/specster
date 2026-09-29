import pytest
import reports  # noqa: F401  (importing the package registers the formats)
from reports.export import to_json
from reports.model import Report
from reports.registry import export, formats

ROWS = [{"name": "a", "amount": "1"}, {"name": "b", "amount": "2"}]


def test_both_formats_are_registered_on_import() -> None:
    assert formats() == ["json", "text"]
    assert export(Report(ROWS), "json") == to_json(Report(ROWS))
    assert export(Report(ROWS), "text") == "name=a, amount=1\nname=b, amount=2\n"


def test_an_unknown_format_names_the_known_ones() -> None:
    with pytest.raises(ValueError, match="json") as e:
        export(Report(ROWS), "xml")
    assert "text" in str(e.value)
