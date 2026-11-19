"""The commentary layer: fact pack, grounding check, writer, and the model fall-back flow."""

from __future__ import annotations

import json
import types

import pytest

from pefund import scenarios as sc
from pefund.ai import llm
from pefund.ai.commentary import answer_question, generate_commentary
from pefund.ai.facts import build_fact_pack, flatten_numbers
from pefund.ai.guardrails import verify_numbers
from pefund.ai.narrator import explain_distribution, lookup_facts, write_commentary
from pefund.ai.prompts import SECTIONS, SYSTEM_PROMPT, commentary_prompt, repair_prompt
from pefund.montecarlo import MonteCarloSettings
from pefund.pipeline import build_bundle

SCENARIO_FILE = "configs/scenarios.yaml"


@pytest.fixture(scope="module")
def bundle(base_config):
    return build_bundle(
        base_config, sc.load_scenarios(SCENARIO_FILE), monte_carlo=MonteCarloSettings(n_sims=120, seed=3)
    )


@pytest.fixture(scope="module")
def pack(bundle):
    return build_fact_pack(bundle)


class FakeClient:
    """Stands in for a language model: returns scripted replies and records the prompts."""

    provider, model = "fake", "fake-1"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, max_tokens: int = 2500) -> str:
        self.calls.append((system, user))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


# --------------------------------------------------------------------------- #
# Fact pack
# --------------------------------------------------------------------------- #
def test_fact_pack_is_json_serialisable_and_compact(pack):
    text = json.dumps(pack)
    assert len(text) < 40_000  # small enough to send with every request
    assert "NaN" not in text


def test_fact_pack_matches_the_model(pack, bundle):
    summary = bundle["summary"]
    perf = pack["performance_at_liquidation"]
    assert perf["net_irr_pct"] == pytest.approx(summary["net_irr"] * 100, abs=0.05)
    assert perf["net_tvpi_x"] == pytest.approx(summary["tvpi"], abs=0.005)
    assert pack["waterfall"]["carry_distributed"] == pytest.approx(summary["carry_gross"], abs=0.05)
    assert len(pack["deals"]) == len(bundle["result"].config.deals)
    assert sum(row["amount"] for row in pack["split_of_gross_profit"]) == pytest.approx(
        perf["total_gross_profit"], abs=0.3
    )


def test_flatten_numbers_ignores_text_and_booleans():
    flat = flatten_numbers({"a": 1.5, "b": {"c_pct": 2, "name": "x", "flag": True}, "d": [3, {"e": 4.0}]})
    assert flat == {"a": 1.5, "b.c_pct": 2.0, "d[0]": 3.0, "d[1].e": 4.0}


# --------------------------------------------------------------------------- #
# Grounding check
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "sentence",
    [
        "LPs earn a net IRR of 15.2% and a net TVPI of 1.83x.",
        "Carried interest totals $96.9m, first paid in December 2029.",
        "Fees cost 307 bps, about 3.1 percentage points of IRR.",
        "The split is 80/20 after a 100% catch-up; see tier 3 and tier 4.",
        "The 5th to 95th percentile range was estimated over 120 trials.",
        "At 30 September 2026 DPI was 0.31x.",
        "The fund has 12 investments and a life of 9.5 years.",
    ],
)
def test_grounded_statements_pass(pack, sentence):
    report = verify_numbers(sentence, pack)
    assert report.passed, report.unverified
    assert report.total >= 1


@pytest.mark.parametrize(
    ("sentence", "bad_token"),
    [
        ("LPs earn a net IRR of 57.3%.", "57.3%"),  # invented figure
        ("LPs earn a net IRR of 15.2x.", "15.2x"),  # right digits, wrong kind
        ("LPs earn a net IRR of 21.0%.", "21.0%"),  # a real figure, but it is the gross IRR
        ("The fund's DPI was 1.14x at the valuation date.", "1.14x"),  # that is the RVPI
        ("Carry is 68.6% of LP-class profit.", None),  # a real percentage; label not one of the checked metrics
        ("Carried interest totals $196.9m.", "$196.9m"),
        ("Carry was first paid in January 2031.", "January 2031"),  # a date the model never produced
        ("The fund was raised in 2019.", "2019"),
        ("Net TVPI is 1.38x.", "1.38x"),  # digits transposed
    ],
)
def test_unsupported_figures_are_caught(pack, sentence, bad_token):
    report = verify_numbers(sentence, pack)
    if bad_token is None:
        # Documented limit of the check: a real figure under an unchecked label passes.
        assert report.passed
        return
    assert not report.passed
    assert [item["token"] for item in report.unverified] == [bad_token]
    assert bad_token in report.summary()


def test_rounding_the_way_the_text_rounds_is_accepted(pack):
    net_irr = pack["performance_at_liquidation"]["net_irr_pct"]  # 15.2
    assert verify_numbers(f"A net IRR of about {round(net_irr)}%.", pack).passed
    assert not verify_numbers("A net IRR of about 40%.", pack).passed  # no net IRR anywhere near this exists


def test_labelled_metrics_accept_any_fact_of_the_same_metric(pack):
    """Scenario and sensitivity values of a metric are legitimate; another metric's value is not."""
    downside = next(s for s in pack["scenarios"] if s["name"] == "Downside")
    assert verify_numbers(f"In the downside case net IRR falls to {downside['net_irr_pct']:.1f}%.", pack).passed
    gross = pack["performance_at_liquidation"]["gross_moic_x"]
    assert verify_numbers(f"The portfolio's gross MOIC of {gross:.2f}x is strong.", pack).passed
    assert not verify_numbers(f"LPs' net TVPI of {gross:.2f}x is strong.", pack).passed


# --------------------------------------------------------------------------- #
# Deterministic writer
# --------------------------------------------------------------------------- #
def test_offline_commentary_has_every_section_and_is_fully_grounded(pack):
    text = write_commentary(pack)
    for title, _ in SECTIONS:
        assert f"## {title}" in text
    report = verify_numbers(text, pack)
    assert report.total > 100
    assert report.passed, report.unverified


@pytest.mark.parametrize(
    "scenario", ["late_losses_deal_by_deal", "severe_downside", "gp_friendly_terms", "subscription_line", "upside"]
)
def test_offline_commentary_stays_grounded_in_other_scenarios(base_config, scenario):
    scenarios = sc.load_scenarios(SCENARIO_FILE)
    cfg = base_config.with_overrides(scenarios[scenario]["overrides"])
    other = build_bundle(cfg, scenarios, monte_carlo=MonteCarloSettings(n_sims=60, seed=5))
    result = generate_commentary(other, provider="offline")
    assert result.grounding.passed, result.grounding.unverified
    assert result.provider == "offline"


def test_offline_commentary_survives_a_fund_that_loses_money(base_config):
    cfg = base_config.with_values({"shocks.exit_multiple_pct": -0.7})
    result = generate_commentary(build_bundle(cfg, None, sensitivities=False, monte_carlo=False), provider="offline")
    assert result.grounding.passed, result.grounding.unverified
    assert "no carried interest" in result.text.lower()


def test_commentary_explains_the_clawback_when_there_is_one(base_config):
    scenarios = sc.load_scenarios(SCENARIO_FILE)
    cfg = base_config.with_overrides(scenarios["late_losses_deal_by_deal"]["overrides"])
    text = generate_commentary(build_bundle(cfg, None, sensitivities=False, monte_carlo=False), provider="offline").text
    assert "clawback test required" in text
    assert "after-tax limit capped" in text


def test_explain_distribution_walks_through_the_tiers(bundle):
    text = explain_distribution(bundle["result"])
    assert "Return of capital" in text and "GP catch-up" in text and "Residual split" in text
    assert "31 December 2029" in text  # the quarter in which carry is first paid
    quiet = explain_distribution(bundle["result"], 0)
    assert "No cash was distributed" in quiet


def test_markdown_footer_reports_provenance(bundle):
    result = generate_commentary(bundle, provider="offline")
    document = result.to_markdown()
    assert document.startswith("# Fund commentary")
    assert "deterministic writer" in document and "Numeric grounding" in document
    assert "not investment, legal or tax advice" in document


# --------------------------------------------------------------------------- #
# Language-model flow
# --------------------------------------------------------------------------- #
def test_prompt_contains_the_rules_the_sections_and_the_facts(pack):
    prompt = commentary_prompt(pack)
    assert "Use only the figures in the FACTS block" in SYSTEM_PROMPT
    assert "<FACTS>" in prompt and "</FACTS>" in prompt
    for title, _ in SECTIONS:
        assert f"## {title}" in prompt
    assert pack["fund"]["name"] in prompt


def test_a_grounded_model_draft_is_published(bundle, pack):
    draft = write_commentary(pack)  # a draft we know is fully grounded
    client = FakeClient([draft])
    result = generate_commentary(bundle, client=client)
    assert (result.provider, result.model) == ("fake", "fake-1")
    assert result.text == draft and not result.used_fallback and result.attempts == 1
    assert client.calls[0][0] == SYSTEM_PROMPT


def test_an_ungrounded_draft_is_sent_back_for_repair(bundle, pack):
    good = write_commentary(pack)
    bad = good.replace("15.2%", "19.9%", 1)
    client = FakeClient([bad, good])
    result = generate_commentary(bundle, client=client)
    assert result.text == good and result.attempts == 2 and not result.used_fallback
    repair_request = client.calls[1][1]
    assert "19.9%" in repair_request and "could not be matched to FACTS" in repair_request


def test_a_draft_that_cannot_be_repaired_falls_back_to_the_writer(bundle, pack):
    bad = write_commentary(pack).replace("15.2%", "19.9%", 1)
    result = generate_commentary(bundle, client=FakeClient([bad, bad]))
    assert result.used_fallback and result.provider == "offline"
    assert result.grounding.passed
    assert "19.9%" not in result.text
    assert any("was rejected" in note for note in result.notes)


def test_a_failed_model_call_falls_back_to_the_writer(bundle):
    result = generate_commentary(bundle, client=FakeClient([llm.LLMError("service unavailable")]))
    assert result.used_fallback and result.grounding.passed
    assert any("service unavailable" in note for note in result.notes)


def test_repair_prompt_lists_the_problems(pack):
    prompt = repair_prompt(pack, "draft text", [{"token": "19.9%", "context": "net IRR of 19.9% and"}])
    assert "'19.9%'" in prompt and "<DRAFT>" in prompt


def test_question_answering_offline_and_with_a_model(bundle, pack):
    offline, _ = answer_question(bundle, "What is the net IRR?", provider="offline")
    assert "net_irr_pct" in offline
    answer, report = answer_question(bundle, "What is the net IRR?", client=FakeClient(["The net IRR is 15.2%."]))
    assert answer == "The net IRR is 15.2%." and report.passed
    _, report = answer_question(bundle, "What is the net IRR?", client=FakeClient(["The net IRR is 31.4%."]))
    assert not report.passed
    assert "no figure matching" in lookup_facts(pack, "zzzz qqqq")


# --------------------------------------------------------------------------- #
# Provider selection and SDK calls
# --------------------------------------------------------------------------- #
@pytest.fixture
def clean_env(monkeypatch):
    for name in ("PEFUND_LLM_PROVIDER", "PEFUND_LLM_MODEL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_no_key_means_offline(clean_env):
    assert llm.detect_provider() == "offline"
    assert llm.get_client() is None


def test_provider_is_detected_from_the_environment(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "test-key")
    client = llm.get_client()
    assert client.provider == "anthropic" and client.model == llm.DEFAULT_ANTHROPIC_MODEL
    clean_env.setenv("PEFUND_LLM_MODEL", "claude-haiku-5-5")
    assert llm.get_client().model == "claude-haiku-5-5"
    clean_env.setenv("PEFUND_LLM_PROVIDER", "offline")
    assert llm.get_client() is None  # an explicit choice wins over a key being present


def test_openai_compatible_provider_needs_a_model_name(clean_env):
    clean_env.setenv("OPENAI_API_KEY", "test-key")
    with pytest.raises(llm.LLMError, match="PEFUND_LLM_MODEL"):
        llm.get_client()
    clean_env.setenv("PEFUND_LLM_MODEL", "some-chat-model")
    assert llm.get_client().model == "some-chat-model"


def test_unknown_provider_and_missing_key_are_reported(clean_env):
    with pytest.raises(llm.LLMError, match="ANTHROPIC_API_KEY"):
        llm.get_client("anthropic")
    clean_env.setenv("PEFUND_LLM_PROVIDER", "telepathy")
    with pytest.raises(llm.LLMError, match="must be one of"):
        llm.detect_provider()


def test_anthropic_client_sends_a_minimal_request_and_reads_text_blocks(monkeypatch):
    anthropic = pytest.importorskip("anthropic")
    seen = {}

    class FakeMessages:
        def create(self, **kwargs):
            seen.update(kwargs)
            blocks = [types.SimpleNamespace(type="thinking", thinking="..."),
                      types.SimpleNamespace(type="text", text="Hello "), types.SimpleNamespace(type="text", text="LPs.")]  # fmt: skip
            return types.SimpleNamespace(content=blocks)

    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key=None: types.SimpleNamespace(messages=FakeMessages()))
    reply = llm.AnthropicClient(model="claude-sonnet-5-5", api_key="k").complete("system text", "user text", 321)
    assert reply == "Hello LPs."
    assert seen == {"model": "claude-sonnet-5-5", "max_tokens": 321, "system": "system text",
                    "messages": [{"role": "user", "content": "user text"}]}  # fmt: skip


def test_openai_compatible_client_sends_system_and_user_messages(monkeypatch):
    openai = pytest.importorskip("openai")
    seen = {}

    class FakeCompletions:
        def create(self, **kwargs):
            seen.update(kwargs)
            message = types.SimpleNamespace(content=" Answer. ")
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    def fake_openai(api_key=None, base_url=None):
        seen["base_url"] = base_url
        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions()))

    monkeypatch.setattr(openai, "OpenAI", fake_openai)
    client = llm.OpenAICompatibleClient(model="m", api_key="k", base_url="http://localhost:11434/v1")
    assert client.complete("sys", "usr") == "Answer."
    assert seen["model"] == "m" and seen["base_url"] == "http://localhost:11434/v1"
    assert seen["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "usr"}]


def test_sdk_errors_become_llm_errors(monkeypatch):
    anthropic = pytest.importorskip("anthropic")

    class Broken:
        def create(self, **kwargs):
            raise RuntimeError("401 invalid x-api-key")

    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key=None: types.SimpleNamespace(messages=Broken()))
    with pytest.raises(llm.LLMError, match="401"):
        llm.AnthropicClient(api_key="k").complete("s", "u")
