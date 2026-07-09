"""Phase G6 (Part 5): the live-LLM eval run. Manually triggered (or
run from a scheduled job with a configured API key) -- never run by
CI, since it needs a real ``ANTHROPIC_API_KEY`` and spends real money
on every invocation.

Measures what only a real model call can measure, none of which the
deterministic harness (``evals/harness.py``, heuristic-only, run in
CI) touches: schema-valid response rate, dispatch accuracy against the
golden set's known-correct answers, the DSL path's first-try validity
rate and repair-loop recovery rate (against a small set of prompts
deliberately chosen to not match any registry generator), and p50/p95
latency. Appends a dated, model-id-stamped entry to ``RESULTS.md`` --
every run's numbers get written down, including a bad run's, per the
brief's own "honest history, not marketing" framing.

Usage::

    ANTHROPIC_API_KEY=sk-... python evals/run_live_eval.py

Not run in this environment: no ``ANTHROPIC_API_KEY`` was configured
here (confirmed throughout this project's own test suite, which always
falls back to the heuristic path for the same reason) -- see
``RESULTS.md`` for the honest "no live run recorded yet" note this
produces until someone with real credentials runs it.
"""

from __future__ import annotations

import datetime
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

MODEL_ID = "claude-fable-5"

# Deliberately shaped to *not* match any registry generator or scene
# pattern -- the whole point is forcing the LLM toward the "dsl" tool,
# the one path this environment's heuristic-only CI run never exercises
# (Phase G3's own rule: the heuristic never attempts DSL synthesis).
DSL_PROMPTS = [
    "a bracket with two mounting holes",
    "an L-shaped bookend",
    "a hexagonal nut shape",
    "a wedge-shaped doorstop",
    "a chess pawn silhouette",
    "a picture frame with a triangular cutout in one corner",
    "an asymmetric decorative wall panel",
    "a custom mechanical linkage arm",
]


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def _fmt_rate(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "n/a (0 cases)"
    return f"{numerator}/{denominator} ({numerator / denominator:.1%})"


def measure_dispatch(cases) -> dict:
    from crdt_cad.ai.interpreter import _llm_interpret

    latencies: list[float] = []
    schema_valid = 0
    dispatch_correct = 0
    dispatch_checked = 0
    errors: list[tuple[str, str]] = []
    for case in cases:
        start = time.monotonic()
        try:
            name, _spec = _llm_interpret(case.prompt)
            latencies.append(time.monotonic() - start)
            schema_valid += 1
            if case.expected_generator is not None:
                dispatch_checked += 1
                if name == case.expected_generator:
                    dispatch_correct += 1
        except Exception as exc:
            latencies.append(time.monotonic() - start)
            errors.append((case.prompt, str(exc)))
    return {
        "total": len(cases),
        "schema_valid": schema_valid,
        "dispatch_checked": dispatch_checked,
        "dispatch_correct": dispatch_correct,
        "latencies": latencies,
        "errors": errors,
    }


def measure_dsl(prompts: list[str]) -> dict:
    from crdt_cad.ai.dsl import DSLError, execute_dsl_program
    from crdt_cad.ai.interpreter import _llm_interpret, llm_repair_dsl_program

    dsl_dispatched = 0
    first_try_valid = 0
    repair_attempted = 0
    repair_recovered = 0
    for prompt in prompts:
        try:
            name, spec = _llm_interpret(prompt)
        except Exception:
            continue
        if name != "dsl":
            continue
        dsl_dispatched += 1
        program = {"root": spec.root, "material": spec.material}
        try:
            execute_dsl_program(program)
            first_try_valid += 1
            continue
        except DSLError as exc:
            repair_attempted += 1
            try:
                repaired = llm_repair_dsl_program(prompt, program, str(exc))
                execute_dsl_program(repaired)
                repair_recovered += 1
            except Exception:
                pass
    return {
        "dsl_dispatched": dsl_dispatched,
        "prompts_tried": len(prompts),
        "first_try_valid": first_try_valid,
        "repair_attempted": repair_attempted,
        "repair_recovered": repair_recovered,
    }


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set -- this script makes real API calls and needs a real key.")
        raise SystemExit(1)

    from crdt_cad.ai.generators import __name__ as _trigger_registration  # noqa: F401
    from evals.golden_prompts import GOLDEN_CASES

    print(f"Running live eval against {MODEL_ID}: {len(GOLDEN_CASES)} golden prompts + {len(DSL_PROMPTS)} DSL prompts...")
    dispatch = measure_dispatch(GOLDEN_CASES)
    dsl = measure_dsl(DSL_PROMPTS)

    p50 = _percentile(dispatch["latencies"], 0.5)
    p95 = _percentile(dispatch["latencies"], 0.95)

    lines = [
        f"## {datetime.date.today().isoformat()} -- {MODEL_ID}",
        "",
        f"- Schema-valid response rate: {_fmt_rate(dispatch['schema_valid'], dispatch['total'])}",
        f"- Dispatch accuracy (of cases with a known-correct generator): {_fmt_rate(dispatch['dispatch_correct'], dispatch['dispatch_checked'])}",
        f"- DSL tool dispatched: {dsl['dsl_dispatched']}/{dsl['prompts_tried']} DSL-shaped prompts",
        f"- DSL first-try validity rate: {_fmt_rate(dsl['first_try_valid'], dsl['dsl_dispatched'])}",
        f"- DSL repair-loop recovery rate: {_fmt_rate(dsl['repair_recovered'], dsl['repair_attempted'])}",
        f"- Latency: p50={p50:.2f}s, p95={p95:.2f}s" if p50 is not None else "- Latency: n/a (no successful calls)",
        f"- Errors: {len(dispatch['errors'])}" + (f" (first: {dispatch['errors'][0]})" if dispatch["errors"] else ""),
        "",
    ]
    entry = "\n".join(lines)
    print(entry)

    results_path = Path(__file__).resolve().parent / "RESULTS.md"
    with open(results_path, "a", encoding="utf-8") as f:
        f.write(entry)
    print(f"Appended to {results_path}")


if __name__ == "__main__":
    main()
