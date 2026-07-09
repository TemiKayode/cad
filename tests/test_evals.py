"""Phase G6 (Part 5): wires the golden-prompt eval harness into the
normal CI pytest run -- fast (no network), deterministic (heuristic
path only, since no ANTHROPIC_API_KEY is configured in CI), a single
score plus a per-category breakdown, and a regression fails the build.
This is the "engineering half" of "shows success": a number CI checks
on every push, not an adjective in a README.

The live-LLM eval (schema-valid rate, dispatch accuracy, DSL first-try
validity, repair recovery, p50/p95 latency) is a separate, manually-
triggered script -- see ``evals/run_live_eval.py`` -- deliberately not
run here, since it needs a real API key and real spend.
"""

import pytest

from crdt_cad.ai import REGISTRY  # noqa: F401 -- triggers registration

from evals.golden_prompts import GOLDEN_CASES
from evals.harness import run_golden_eval

# The three categories with an exact, deterministic expected outcome
# (a keyword substring match or a regex field extraction, no judgment
# call involved) are held to a perfect bar -- any failure here is a
# real regression, not noise. The other three categories test *graceful
# degradation* (ambiguous prompts falling back to house, adversarial
# input never crashing, non-English input not silently mis-dispatching
# into something broken) where a small amount of slack avoids the eval
# becoming brittle against a future, still-correct change in keyword
# coverage.
_STRICT_CATEGORIES = ("registry", "house-dimensioned", "scene")
_LENIENT_CATEGORY_MIN_SCORE = 0.85
_OVERALL_MIN_SCORE = 0.95


@pytest.fixture(scope="module")
def report():
    # Run once and shared across every assertion below -- 66 cases each
    # doing a real interpret+build+validate is real work; re-running the
    # whole set per test would make this the slowest file in the suite
    # for no benefit, since the result is 100% deterministic (heuristic
    # path only, no network) within one test session.
    return run_golden_eval(GOLDEN_CASES)


def test_golden_prompt_set_has_at_least_sixty_cases():
    assert len(GOLDEN_CASES) >= 60


def test_golden_eval_overall_score_meets_the_bar(report):
    assert report.score >= _OVERALL_MIN_SCORE, (
        f"eval score {report.score:.1%} ({report.passed}/{report.total}) "
        f"below the {_OVERALL_MIN_SCORE:.0%} bar:\n{report.format_failures()}"
    )


def test_golden_eval_deterministic_categories_are_perfect(report):
    by_category = report.by_category()
    for category in _STRICT_CATEGORIES:
        passed, total = by_category.get(category, (0, 0))
        assert total > 0, f"category {category!r} has no cases at all"
        assert passed == total, (
            f"category {category!r}: {passed}/{total} -- expected every case to pass "
            f"(deterministic dispatch/extraction, no judgment call involved)\n"
            + "\n".join(
                f"- {r.case.prompt!r}: {'; '.join(r.reasons)}"
                for r in report.failures if r.case.category == category
            )
        )


def test_golden_eval_degradation_categories_stay_above_the_lenient_bar(report):
    by_category = report.by_category()
    for category, (passed, total) in by_category.items():
        if category in _STRICT_CATEGORIES:
            continue
        score = passed / total if total else 1.0
        assert score >= _LENIENT_CATEGORY_MIN_SCORE, (
            f"category {category!r}: {passed}/{total} ({score:.1%}) below the "
            f"{_LENIENT_CATEGORY_MIN_SCORE:.0%} graceful-degradation bar"
        )


def test_every_golden_case_that_expects_a_generator_names_a_real_one():
    for case in GOLDEN_CASES:
        if case.expected_generator is not None and case.expected_generator != "scene":
            assert case.expected_generator in REGISTRY, f"{case.prompt!r} expects unknown generator {case.expected_generator!r}"
