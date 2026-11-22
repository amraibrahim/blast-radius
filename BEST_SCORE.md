# Development record

Sample set: 30 commits. Oracle (failing tests only, best worker split) = 94.351 s.
"Est. board" = simulated + local CPU x 2.2 (median recorded/local test-time ratio; scorer CPU speed unverified).

| Version | Simulated | Misses | Local CPU | Est. board | Key change |
|---|---|---|---|---|---|
| starter (run everything, 3 workers) | ~1614 | 0 | - | 1614.1 (published) | - |
| previous agent | 1104.510 | 0 | ~2 s | ~1109 | coverage index from an interrupted run (514 tests missing); Dockerfile did not ship the index (would be rejected) |
| v1 static | 334.956 | 0 | 1.9 | ~339 | complete per-test coverage via sys.monitoring; statement-level regions; class instantiation map |
| v2 static | 174.010 | 0 | 0.6 | ~175 | class-body edits -> instantiating tests; replacements are not insertions |
| v3 static | 169.219 | 0 | 1.2 | 171.8 | attribute-reader intersection (TSQL.WEEK_OFFSET: 171 -> 7 candidates); eager-import index |
| **v4 static + verify** | **162.103** | **0** | **3.1** | **~169** | run cheap candidates against the patched tree; warm forks; cost model with margins |

Validated: 30/30 sample commits where the verifier's failures equal the labeled failures exactly
(tools/fidelity.py, running candidates + true failures).

## Rebuild the index (only needed if the tooling changes; the committed index.json.gz is current)

    python -m venv .build/venv && .build/venv/bin/pip install -r requirements.lock   # or the subset used here
    PATH=$PWD/.build/venv/bin:$PATH .build/venv/bin/python tools/build_coverage.py .build/sqlglot .build/out/cov.json
    .build/venv/bin/python tools/measure_cpu.py .build/sqlglot sample/tests.json .build/out/cpu.json
    .build/venv/bin/python tools/core_modules.py .build/sqlglot .build/out/core.json
    SQLGLOT_ROOT=.build/sqlglot python3 tools/compact_index.py .build/out/cov.json index.json.gz .build/out/cpu.json .build/out/core.json

## Dev commands

    python benchmark.py [--static]     # native sample score + per-commit excess (BR_VERBOSE=1 for reasons)
    python test_selector.py            # unit + sample-safety checks
    python tools/edge_cases.py         # hand-made unusual patches through the static analysis
    python tools/fidelity.py [ids]     # verifier vs. labeled failures
    python tools/mutants.py N out.json # synthetic-mutant calibration (pinned repo only)

## Remaining excess (67.8 s), all on zero-failure commits

- 2haz (25.5 s), 66ks (24.8 s): equivalent mutations on lines that test_execute_tpcds (21.5 s) covers.
  Running tpcds costs about as much as listing it. Dropping it when cheaper co-covering tests pass was
  calibrated on synthetic mutants: 0 solo tpcds failures in 121 mutants, but only 2 cases where the
  cheaper tests all passed. That is too little evidence to risk 300 s, so it stays listed.
- 4d63 (12.7 s): Snowflake.Tokenizer.NUMERIC_ESCAPES is consumed by the dialect metaclass at class
  creation, so every Snowflake user is a genuine candidate.
