from dataclasses import dataclass, field


@dataclass
class Report:
    rows: list[dict[str, str]] = field(default_factory=list)

    def total(self) -> int:
        return len(self.rows)
