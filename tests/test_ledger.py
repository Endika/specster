import threading

from specster.config import ModelConfig
from specster.ledger import Ledger
from specster.llm.base import Usage

MTOK = Usage(1_000_000, 0, 0, 0)


def test_costs_are_summed_per_role_from_many_threads() -> None:
    led = Ledger({})
    worker = ModelConfig(model="claude-sonnet-5")
    threads = [threading.Thread(target=led.add, args=("worker", worker, MTOK, 1)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    led.add("reviewer", ModelConfig(model="claude-opus-5-5"), MTOK, 2)
    roles = led.roles()
    assert roles["worker"].cost_usd == 16.0 and roles["worker"].turns == 8
    assert led.cost() == 20.0 and led.turns() == 10 and led.usage().input_tokens == 9_000_000


def test_an_unpriced_role_makes_the_total_unknown_but_keeps_the_known_part() -> None:
    led = Ledger({})
    led.add("worker", ModelConfig(provider="openai", model="gpt-x"), MTOK, 1)
    led.add("reviewer", ModelConfig(model="claude-opus-5-5"), MTOK, 1)
    assert led.cost() is None and led.known_cost() == 4.0 and led.unpriced() == ["worker"]


def test_a_role_with_unknown_usage_makes_the_total_unknown() -> None:
    led = Ledger({})
    led.add("reviewer", ModelConfig(model="claude-opus-5-5"), MTOK, 1)
    led.mark_unknown("worker", ModelConfig(model="claude-sonnet-5"))
    assert led.cost() is None and led.known_cost() == 4.0
    assert led.roles()["worker"].cost_usd is None and led.unpriced() == []
