import json

from reports.model import Report


def to_json(report: Report) -> str:
    return json.dumps(report.rows)
