from specster.config import PersonaConfig
from specster.prompts import system_prompt


def test_concise_is_the_default_and_caps_questions() -> None:
    prompt = system_prompt(PersonaConfig(), [])
    assert "Be concise" in prompt
    assert "Ask at most 3 questions" in prompt


def test_detailed_style_and_custom_question_cap() -> None:
    prompt = system_prompt(PersonaConfig(style="detailed", max_questions=5), [])
    assert "Be concise" not in prompt
    assert "Ask at most 5 questions" in prompt


def test_thread_is_declared_untrusted() -> None:
    assert "untrusted data" in system_prompt(PersonaConfig(), [])
