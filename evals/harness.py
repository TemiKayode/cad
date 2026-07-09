"""Phase G6 (Part 5): scoring logic for the golden prompt set. Runs the
*real* pipeline (``interpret_prompt`` -> ``generate_ops_from_interpretation``)
on every case -- deliberately not a reimplementation of dispatch/build
logic, so the eval measures the actual system a user would hit, not a
parallel copy of it that could silently drift out of sync.

Every call here goes through ``interpret_prompt``, which always tries
the LLM path first and falls back to the heuristic on any failure (see
``interpreter.py``'s own docstring) -- so running this harness with no
``ANTHROPIC_API_KEY`` configured (the case in CI, and the case this
module's own test asserts against) exercises the heuristic path
end-to-end, honestly, with no special-casing needed here to force it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from crdt_cad.ai.generator import generate_ops_from_interpretation
from crdt_cad.ai.interpreter import interpret_prompt

from evals.golden_prompts import GoldenCase


@dataclass
class GoldenCaseResult:
    case: GoldenCase
    passed: bool
    generator_name: str
    reasons: list[str] = field(default_factory=list)


@dataclass
class EvalReport:
    results: list[GoldenCaseResult]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def score(self) -> float:
        return self.passed / self.total if self.total else 1.0

    @property
    def failures(self) -> list[GoldenCaseResult]:
        return [r for r in self.results if not r.passed]

    def by_category(self) -> dict[str, tuple[int, int]]:
        counts: dict[str, list[int]] = {}
        for r in self.results:
            bucket = counts.setdefault(r.case.category, [0, 0])
            bucket[1] += 1
            if r.passed:
                bucket[0] += 1
        return {cat: (passed, total) for cat, (passed, total) in counts.items()}

    def format_failures(self) -> str:
        return "\n".join(
            f"- [{r.case.category}] {r.case.prompt!r}: {'; '.join(r.reasons)}" for r in self.failures
        )


def evaluate_case(case: GoldenCase) -> GoldenCaseResult:
    reasons: list[str] = []
    try:
        generator_name, spec, source = interpret_prompt(case.prompt)
    except Exception as exc:  # pragma: no cover - interpret_prompt already catches broadly internally
        return GoldenCaseResult(case=case, passed=False, generator_name="<error>", reasons=[f"interpret_prompt raised: {exc}"])

    if case.expected_generator is not None and generator_name != case.expected_generator:
        reasons.append(f"expected generator {case.expected_generator!r}, got {generator_name!r}")

    for field_name, expected_value in case.expected_spec.items():
        actual = getattr(spec, field_name, "<missing>")
        if actual != expected_value:
            reasons.append(f"expected spec.{field_name}={expected_value!r}, got {actual!r}")

    if case.require_valid_geometry:
        try:
            result = generate_ops_from_interpretation(case.prompt, generator_name, spec, source)
            if not result.validation.ok:
                reasons.append(f"geometry invalid: {result.validation.errors}")
            if not result.ops:
                reasons.append("generation produced zero ops")
        except Exception as exc:
            reasons.append(f"generation raised: {exc}")

    return GoldenCaseResult(case=case, passed=not reasons, generator_name=generator_name, reasons=reasons)


def run_golden_eval(cases: list[GoldenCase]) -> EvalReport:
    return EvalReport(results=[evaluate_case(c) for c in cases])
