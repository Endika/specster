from reports.amounts import parse_amount
from reports.model import Report


def sum_amounts(report: Report) -> float:
    return sum(parse_amount(row["amount"]) for row in report.rows if "amount" in row)
