import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

EVALS = Path(__file__).resolve().parent.parent / "evals"
FIXTURE = EVALS / "fixtures" / "build_repo"
CHECKS = EVALS / "fixtures" / "build_checks"

# One right answer per case: the hidden check must pass on it and fail on the untouched repo,
# or the benchmark would grade workers against a broken yardstick.
REFERENCE = {
    "csv-export": {
        "reports/export.py": (
            "import csv\nimport io\nimport json\n\nfrom reports.model import Report\n\n\n"
            "def to_json(report: Report) -> str:\n    return json.dumps(report.rows)\n\n\n"
            "def to_csv(report: Report) -> str:\n"
            "    if not report.rows:\n        return ''\n"
            "    header = list(dict.fromkeys(k for row in report.rows for k in row))\n"
            "    out = io.StringIO()\n"
            "    writer = csv.DictWriter(out, header, restval='', lineterminator='\\n')\n"
            "    writer.writeheader()\n    writer.writerows(report.rows)\n"
            "    return out.getvalue()\n"
        ),
    },
    "two-exporters": {
        "reports/tsv.py": (
            "from reports.model import Report\n\n\n"
            "def to_tsv(report: Report) -> str:\n"
            "    if not report.rows:\n        return ''\n"
            "    keys = list(report.rows[0])\n"
            "    lines = ['\\t'.join(keys)]\n"
            "    lines += ['\\t'.join(r[k] for k in keys) for r in report.rows]\n"
            "    return ''.join(line + '\\n' for line in lines)\n"
        ),
        "reports/markdown.py": (
            "from reports.model import Report\n\n\n"
            "def to_markdown(report: Report) -> str:\n"
            "    if not report.rows:\n        return ''\n"
            "    keys = list(report.rows[0])\n"
            "    lines = ['| ' + ' | '.join(keys) + ' |']\n"
            "    lines += ['| ' + ' | '.join('---' for _ in keys) + ' |']\n"
            "    lines += ['| ' + ' | '.join(r[k] for k in keys) + ' |' for r in report.rows]\n"
            "    return '\\n'.join(lines)\n"
        ),
    },
    "rename-with-alias": {
        "reports/model.py": (
            "import warnings\n\n\nclass Report:\n"
            "    def __init__(self, records=None, *, rows=None):\n"
            "        if rows is not None:\n"
            "            warnings.warn('rows is now records', DeprecationWarning, stacklevel=2)\n"
            "            records = rows\n"
            "        self.records = list(records) if records is not None else []\n\n"
            "    @property\n    def rows(self):\n"
            "        warnings.warn('rows is now records', DeprecationWarning, stacklevel=2)\n"
            "        return self.records\n"
        ),
        "reports/export.py": (
            "import json\n\nfrom reports.model import Report\n\n\n"
            "def to_json(report: Report) -> str:\n    return json.dumps(report.records)\n"
        ),
        "reports/totals.py": (
            "from reports.amounts import parse_amount\nfrom reports.model import Report\n\n\n"
            "def sum_amounts(report: Report) -> float:\n"
            "    return sum(parse_amount(r['amount']) for r in report.records if 'amount' in r)\n"
        ),
    },
    "bug-fix-from-symptom": {
        "reports/amounts.py": (
            "def parse_amount(text: str) -> float:\n"
            "    text = text.strip()\n"
            "    if text.startswith('(') and text.endswith(')'):\n"
            "        return -parse_amount(text[1:-1])\n"
            "    return float(text.replace(',', ''))\n"
        ),
    },
    "csv-round-trip": {
        "reports/csv_io.py": (
            "import csv\nimport io\n\nfrom reports.model import Report\n\n\n"
            "def to_csv(report: Report) -> str:\n"
            "    if not report.rows:\n        return ''\n"
            "    header = list(dict.fromkeys(k for row in report.rows for k in row))\n"
            "    out = io.StringIO()\n"
            "    writer = csv.DictWriter(out, header, restval='', lineterminator='\\n')\n"
            "    writer.writeheader()\n    writer.writerows(report.rows)\n"
            "    return out.getvalue()\n\n\n"
            "def from_csv(text: str) -> Report:\n"
            "    text = text.removeprefix('\\ufeff')\n"
            "    if not text:\n        return Report([])\n"
            "    reader = csv.DictReader(io.StringIO(text, newline=''), restval='')\n"
            "    return Report([dict(row) for row in reader])\n"
        ),
    },
    "exporter-registry": {
        "reports/registry.py": (
            "from collections.abc import Callable\n\nfrom reports.model import Report\n\n"
            "_EXPORTERS: dict[str, Callable[[Report], str]] = {}\n\n\n"
            "def register(name: str):\n"
            "    def add(fn: Callable[[Report], str]) -> Callable[[Report], str]:\n"
            "        _EXPORTERS[name] = fn\n        return fn\n"
            "    return add\n\n\n"
            "def formats() -> list[str]:\n    return sorted(_EXPORTERS)\n\n\n"
            "def export(report: Report, name: str) -> str:\n"
            "    if name not in _EXPORTERS:\n"
            "        raise ValueError(f'unknown format {name}; known: {formats()}')\n"
            "    return _EXPORTERS[name](report)\n"
        ),
        "reports/export.py": (
            "import json\n\nfrom reports.model import Report\n"
            "from reports.registry import register\n\n\n"
            "@register('json')\ndef to_json(report: Report) -> str:\n"
            "    return json.dumps(report.rows)\n"
        ),
        "reports/text.py": (
            "from reports.model import Report\nfrom reports.registry import register\n\n\n"
            "@register('text')\ndef to_text(report: Report) -> str:\n"
            "    lines = (', '.join(f'{k}={v}' for k, v in r.items()) for r in report.rows)\n"
            "    return ''.join(line + '\\n' for line in lines)\n"
        ),
        "reports/__init__.py": "from reports import export, text  # noqa: F401\n",
    },
    "total-then-summary": {
        "reports/model.py": (
            "from dataclasses import dataclass, field\n\n\n@dataclass\nclass Report:\n"
            "    rows: list[dict[str, str]] = field(default_factory=list)\n\n"
            "    def total(self, name: str) -> float:\n"
            "        return sum((float(r[name]) for r in self.rows if name in r), 0.0)\n"
        ),
        "reports/export.py": (
            "import json\n\nfrom reports.model import Report\n\n\n"
            "def to_json(report: Report) -> str:\n    return json.dumps(report.rows)\n\n\n"
            "def summary(report: Report) -> str:\n"
            "    total = report.total('amount')\n"
            "    return f'{len(report.rows)} rows, total amount {total:.2f}'\n"
        ),
    },
}
CASES = {c["id"]: c for c in yaml.safe_load((EVALS / "build_cases.yaml").read_text())}


def _hidden_check(repo: Path, hidden: str) -> subprocess.CompletedProcess[str]:
    shutil.copy(CHECKS / hidden, repo / "tests" / hidden)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"tests/{hidden}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def test_every_case_has_a_reference_answer_and_a_hidden_check() -> None:
    assert set(CASES) == set(REFERENCE)
    for case in CASES.values():
        assert (CHECKS / case["hidden"]).is_file()


@pytest.mark.parametrize("case_id", sorted(REFERENCE))
def test_the_hidden_check_fails_on_the_base_and_passes_on_the_reference(
    case_id: str, tmp_path: Path
) -> None:
    hidden = CASES[case_id]["hidden"]
    base = Path(shutil.copytree(FIXTURE, tmp_path / "base"))
    assert _hidden_check(base, hidden).returncode != 0
    solved = Path(shutil.copytree(FIXTURE, tmp_path / "solved"))
    for rel, text in REFERENCE[case_id].items():
        (solved / rel).write_text(text)
    result = _hidden_check(solved, hidden)
    assert result.returncode == 0, result.stdout + result.stderr
