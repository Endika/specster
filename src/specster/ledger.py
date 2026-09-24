import threading
from collections.abc import Mapping

from specster.config import ModelConfig, PriceEntry
from specster.llm.base import Usage
from specster.metrics import RoleMetrics
from specster.pricing import cost_usd


class Ledger:
    def __init__(self, pricing: Mapping[str, PriceEntry]) -> None:
        self._pricing = pricing
        self._lock = threading.Lock()
        self._roles: dict[str, tuple[ModelConfig, Usage, int]] = {}
        # Roles whose spend is only partly known (a run died without reporting its usage).
        self._unknown: set[str] = set()

    def add(self, role: str, cfg: ModelConfig, usage: Usage, turns: int) -> None:
        with self._lock:
            _, before, t = self._roles.get(role, (cfg, Usage(), 0))
            self._roles[role] = (cfg, before + usage, t + turns)

    def mark_unknown(self, role: str, cfg: ModelConfig) -> None:
        with self._lock:
            self._roles.setdefault(role, (cfg, Usage(), 0))
            self._unknown.add(role)

    def roles(self) -> dict[str, RoleMetrics]:
        with self._lock:
            items = list(self._roles.items())
            unknown = set(self._unknown)
        return {
            role: RoleMetrics(
                provider=cfg.provider,
                model=cfg.model,
                input_tokens=u.input_tokens,
                cache_read_tokens=u.cache_read_tokens,
                cache_write_tokens=u.cache_write_tokens,
                output_tokens=u.output_tokens,
                cost_usd=None
                if role in unknown
                else cost_usd(cfg.provider, cfg.model, u, self._pricing),
                turns=turns,
            )
            for role, (cfg, u, turns) in items
        }

    def usage(self) -> Usage:
        with self._lock:
            items = list(self._roles.values())
        total = Usage()
        for _, u, _ in items:
            total = total + u
        return total

    def turns(self) -> int:
        with self._lock:
            items = list(self._roles.values())
        return sum(t for _, _, t in items)

    def _priced(self) -> dict[str, float | None]:
        with self._lock:
            items = list(self._roles.items())
        return {
            role: cost_usd(cfg.provider, cfg.model, u, self._pricing) for role, (cfg, u, _) in items
        }

    def known_cost(self) -> float:
        return round(sum(c for c in self._priced().values() if c is not None), 6)

    def unpriced(self) -> list[str]:
        return sorted(role for role, c in self._priced().items() if c is None)

    def cost(self) -> float | None:
        with self._lock:
            unknown = bool(self._unknown)
        return None if unknown or self.unpriced() else self.known_cost()
