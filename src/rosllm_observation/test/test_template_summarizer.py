from rosllm_observation.template_summarizer import render_summary


def test_render_empty_history():
    text = render_summary("A sensor.", [])
    assert "# Sensor" in text
    assert "A sensor." in text
    assert "The past 0 measurements" in text


def test_render_one_and_many():
    single = render_summary("d", [[1.0, 2.0]])
    assert "The past 1 measurement\n" in single
    many = render_summary("d", [[1.0], [2.0], [3.0]])
    assert "The past 3 measurements" in many
    assert "[1.0]" in many and "[3.0]" in many
