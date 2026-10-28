from rosllm_agent.prompt import (
    MAX_PLAN_TEXT_BYTES,
    build_prompt,
    extract_plan_text,
)


def test_build_prompt_contains_all_sections():
    prompt = build_prompt("ACTION LIBRARY", "OBS", "TASK", "ADVICE")
    assert "ACTION LIBRARY" in prompt
    assert "OBS" in prompt
    assert "TASK" in prompt
    assert "# Expert advice" in prompt and "ADVICE" in prompt
    assert "json lines" in prompt


def test_build_prompt_without_advice():
    prompt = build_prompt("A", "O", "T")
    assert "# Expert advice" not in prompt


def test_extract_fenced_json():
    text = 'prose\n```json\n{"name": "forward"}\n{"name": "turn"}\n```\ntail'
    assert extract_plan_text(text) == '{"name": "forward"}\n{"name": "turn"}'


def test_extract_multiple_fences_and_unlabelled():
    text = '```json\n{"a": 1}\n```\n```\n{"b": 2}\n```'
    assert extract_plan_text(text) == '{"a": 1}\n{"b": 2}'


def test_extract_ignores_other_languages():
    text = '```python\nprint(1)\n```'
    # No json fence: the whole response is returned and later rejected by the
    # strict plan parser rather than silently treating Python as a plan.
    assert extract_plan_text(text).startswith("```python")


def test_extract_plain_response():
    assert extract_plan_text('{"name": "forward"}') == '{"name": "forward"}'


def test_extract_empty_and_non_string():
    assert extract_plan_text("") == ""
    assert extract_plan_text(None) == ""


def test_extract_truncates_oversized_text():
    text = "x" * (MAX_PLAN_TEXT_BYTES + 100)
    assert len(extract_plan_text(text)) == MAX_PLAN_TEXT_BYTES
