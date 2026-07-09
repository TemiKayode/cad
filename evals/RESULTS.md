# Live-LLM eval results (Phase G6, Part 5)

Honest history, not marketing: every run of `evals/run_live_eval.py`
appends its numbers here, unedited, including a bad run's. Each entry
is dated and stamped with the model id that produced it.

**No live run has been recorded in this environment yet.** No
`ANTHROPIC_API_KEY` was configured anywhere in this project's
development (confirmed throughout the test suite, which always falls
back to the heuristic path for exactly that reason -- see
`interpreter.py`'s own docstring and every `LLM ... unavailable ...
using the heuristic dispatcher` log line in this repo's test output).
The deterministic eval (`evals/harness.py`, wired into CI via
`tests/test_evals.py`) runs on every push and is what this project can
actually claim right now: see the README's "AI quality" section for
its latest score.

To produce a real entry below, run:

```
ANTHROPIC_API_KEY=sk-... python evals/run_live_eval.py
```

This measures, against the same 66-case golden prompt set
(`evals/golden_prompts.py`) plus 8 prompts deliberately shaped to miss
every registry generator (forcing the DSL path): schema-valid response
rate, dispatch accuracy, DSL first-try validity rate, DSL repair-loop
recovery rate, and p50/p95 latency.

<!-- New entries are appended below this line by run_live_eval.py -->
