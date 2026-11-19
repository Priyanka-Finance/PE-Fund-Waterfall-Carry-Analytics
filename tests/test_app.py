"""Smoke tests for the Streamlit dashboard, run headlessly."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
pytest.importorskip("plotly")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = str(Path(__file__).resolve().parents[1] / "app" / "streamlit_app.py")


@pytest.fixture(scope="module")
def app():
    at = AppTest.from_file(APP, default_timeout=180)
    at.run()
    return at


def metric(at: AppTest, label: str) -> str:
    return next(m.value for m in at.metric if m.label == label)


def test_dashboard_renders_the_base_case(app):
    assert not app.exception
    assert app.title[0].value == "Private equity fund waterfall and carry analytics"
    assert metric(app, "Net IRR") == "15.2%"
    assert metric(app, "Net TVPI") == "1.83x"
    assert metric(app, "Gross MOIC") == "2.39x"
    assert [tab.label for tab in app.tabs][:3] == ["Overview", "Waterfall", "Deals"]


def test_sidebar_controls_rerun_the_model(app):
    app.sidebar.radio[0].set_value("american").run()
    assert not app.exception
    assert metric(app, "Net IRR") == "14.5%"  # deal-by-deal pays carry earlier, so LPs earn less
    carry = next(s for s in app.sidebar.slider if s.label == "Carried interest (%)")
    carry.set_value(25.0).run()
    assert not app.exception
    assert metric(app, "Net IRR") != "14.5%"
    carry.set_value(20.0).run()
    app.sidebar.radio[0].set_value("european").run()
    assert metric(app, "Net IRR") == "15.2%"


def test_commentary_button_writes_grounded_text(app, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "PEFUND_LLM_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    next(b for b in app.button if b.label == "Write commentary").click().run()
    assert not app.exception
    assert any("Numeric grounding" in s.value and "100%" in s.value for s in app.success)
    assert any("## Executive summary" in m.value for m in app.markdown)


def test_analysis_buttons_run_without_error(app):
    for label in ("Run the two-way table", "Run the tornado"):
        next(b for b in app.button if b.label == label).click().run()
        assert not app.exception, label
