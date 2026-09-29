import json

from reports.export import to_json
from reports.model import Report


def test_to_json_writes_the_rows() -> None:
    report = Report([{"name": "a", "amount": "1"}])
    assert json.loads(to_json(report)) == [{"name": "a", "amount": "1"}]
