"""Orchestrate the commentary: facts -> draft -> verify -> repair -> fall back.

The flow is the same whichever back-end writes the draft:

1. build the fact pack from the model outputs;
2. write a draft (language model, or the deterministic writer when offline);
3. verify every figure and date in the draft against the fact pack;
4. if a model's draft fails, ask the model once to repair it;
5. if it still fails, publish the deterministic writer's text instead.

So the published commentary is always fully reconciled to the numbers.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from . import prompts
from .facts import build_fact_pack
from .guardrails import GroundingReport, verify_numbers
from .llm import LLMClient, LLMError, get_client
from .narrator import lookup_facts, write_commentary


@dataclass
class CommentaryResult:
    """Commentary text plus how it was produced and how well it is grounded."""

    text: str
    provider: str
    model: str
    grounding: GroundingReport
    fact_pack: dict[str, Any]
    used_fallback: bool = False
    attempts: int = 1
    notes: list[str] = field(default_factory=list)

    def to_markdown(self, title: str | None = None) -> str:
        """Commentary with a title and a provenance footer."""
        fund = self.fact_pack["fund"]["name"]
        writer = "deterministic writer (no language model)" if self.provider == "offline" else f"{self.provider} / {self.model}"
        lines = [f"# {title or 'Fund commentary: ' + fund}", "", self.text, "", "---", ""]
        lines.append(f"*Written by: {writer}. Generated {dt.date.today():%d %B %Y}.*  ")
        lines.append(f"*Numeric grounding: {self.grounding.summary()}*  ")
        for note in self.notes:
            lines.append(f"*{note}*  ")
        lines.append("*Hypothetical fund. Analytical illustration only; not investment, legal or tax advice.*")
        return "\n".join(lines) + "\n"


def generate_commentary(
    bundle: dict[str, Any],
    provider: str | None = None,
    model: str | None = None,
    *,
    client: LLMClient | None = None,
    max_repairs: int = 1,
) -> CommentaryResult:
    """Write grounded commentary for a pipeline bundle.

    Parameters
    ----------
    provider, model
        Back-end to use; auto-detected from the environment when omitted
        (``offline`` if no API key is set).
    client
        A ready-made client, mainly for tests.
    max_repairs
        How many times a model may be asked to fix unverified figures.
    """
    pack = build_fact_pack(bundle)
    notes: list[str] = []
    if client is None:
        try:
            client = get_client(provider, model)
        except LLMError as exc:
            notes.append(f"Language model unavailable ({exc}); used the deterministic writer.")
            client = None

    if client is None:
        text = write_commentary(pack)
        return CommentaryResult(text, "offline", "rule-based", verify_numbers(text, pack), pack, notes=notes)

    attempts = 0
    try:
        draft = client.complete(prompts.SYSTEM_PROMPT, prompts.commentary_prompt(pack))
        attempts = 1
        report = verify_numbers(draft, pack)
        while not report.passed and attempts <= max_repairs:
            draft = client.complete(prompts.SYSTEM_PROMPT, prompts.repair_prompt(pack, draft, report.unverified))
            attempts += 1
            report = verify_numbers(draft, pack)
    except LLMError as exc:
        notes.append(f"Language model call failed ({exc}); used the deterministic writer.")
        text = write_commentary(pack)
        return CommentaryResult(text, "offline", "rule-based", verify_numbers(text, pack), pack, True, attempts, notes)

    if report.passed and draft.strip():
        return CommentaryResult(draft, client.provider, client.model, report, pack, False, attempts, notes)

    notes.append(
        f"The {client.provider} / {client.model} draft was rejected after {attempts} attempt(s): "
        f"{len(report.unverified)} figure(s) could not be verified. The deterministic writer's text is shown instead."
    )
    text = write_commentary(pack)
    return CommentaryResult(text, "offline", "rule-based", verify_numbers(text, pack), pack, True, attempts, notes)


def answer_question(
    bundle: dict[str, Any],
    question: str,
    provider: str | None = None,
    model: str | None = None,
    *,
    client: LLMClient | None = None,
) -> tuple[str, GroundingReport]:
    """Answer a free-form question about the fund from the fact pack."""
    pack = build_fact_pack(bundle)
    if client is None:
        try:
            client = get_client(provider, model)
        except LLMError:
            client = None
    if client is None:
        answer = lookup_facts(pack, question)
        return answer, GroundingReport()
    try:
        answer = client.complete(prompts.SYSTEM_PROMPT, prompts.question_prompt(pack, question), max_tokens=600)
    except LLMError as exc:
        return f"Language model call failed ({exc}).\n\n" + lookup_facts(pack, question), GroundingReport()
    return answer, verify_numbers(answer, pack)
