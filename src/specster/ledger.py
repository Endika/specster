import threading
from collections.abc import Mapping

from specster.config import ModelConfig, PriceEntry
from specster.llm.base import Usage
from specster.metrics import RoleMetrics
from specster.pricing import cost_usd


class Meter:
    """A running loop's spend: counted by known_cost until the loop is billed and closes it."""

    def __init__(self, cfg: ModelConfig, lock: threading.Lock, running: set["Meter"]) -> None:
        self.cfg = cfg
        self.usage = Usage()
        self.turns = 0
        self._lock = lock
        self._running = running

    def turn(self, usage: Usage) -> None:
        with self._lock:
            self.usage = self.usage + usage
            self.turns += 1

    def close(self) -> None:
        with self._lock:
            self._running.discard(self)


class Ledger:
    def __init__(self, pricing: Mapping[str, PriceEntry]) -> None:
        self._pricing = pricing
        self._lock = threading.Lock()
        self._roles: dict[str, tuple[ModelConfig, Usage, int]] = {}
        # Roles whose spend is only partly known (a run died without reporting its usage).
        self._unknown: set[str] = set()
        self._running: set[Meter] = set()

    def add(self, role: str, cfg: ModelConfig, usage: Usage, turns: int) -> None:
        with self._lock:
            _, before, t = self._roles.get(role, (cfg, Usage(), 0))
            self._roles[role] = (cfg, before + usage, t + turns)

    def meter(self, cfg: ModelConfig) -> Meter:
        meter = Meter(cfg, self._lock, self._running)
        with self._lock:
            self._running.add(meter)
        return meter

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
        with self._lock:
            spent = [(cfg, u) for cfg, u, _ in self._roles.values()]
            spent += [(m.cfg, m.usage) for m in self._running]
        priced = [cost_usd(cfg.provider, cfg.model, u, self._pricing) for cfg, u in spent]
        return round(sum(c for c in priced if c is not None), 6)

    def unpriced(self) -> list[str]:
        return sorted(role for role, c in self._priced().items() if c is None)

    def cost(self) -> float | None:
        with self._lock:
            unknown = bool(self._unknown)
        return None if unknown or self.unpriced() else self.known_cost()
