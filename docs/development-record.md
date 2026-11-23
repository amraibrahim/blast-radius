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
| v4 static + verify | 162.103 | 0 | 3.1 | ~169 | run cheap candidates against the patched tree; warm forks; cost model with margins |
| v5 (practice board 177.3 for an earlier build) | 109.784 | 0 | 2.4 | ~115 | comparison profile; measured child overheads; schedule lower-bound pruning |
| **v6** | **97.098** | **0** | **~2.4** | **~102.5** | class-table access profile; PY_START coverage (one-line lambdas/defs); cache-fill rule; verifier failure evidence handled in full |

Validated: 30/30 sample commits where the verifier's failures equal the labeled failures exactly
(tools/fidelity.py, running candidates + true failures).

## Rebuild the index (only needed if the tooling changes; the committed index.json.gz is current)

    python -m venv .build/venv && .build/venv/bin/pip install -r requirements.lock   # or the subset used here
    PATH=$PWD/.build/venv/bin:$PATH .build/venv/bin/python tools/build_coverage.py .build/sqlglot .build/out/cov.json
    .build/venv/bin/python tools/build_cmp_profile.py .build/sqlglot .build/out/cmp.json
    PATH=$PWD/.build/venv/bin:$PATH .build/venv/bin/python tools/build_table_profile.py .build/sqlglot .build/out/tables.json
    .build/venv/bin/python tools/measure_cpu.py .build/sqlglot sample/tests.json .build/out/cpu.json
    .build/venv/bin/python tools/core_modules.py .build/sqlglot .build/out/core.json
    CMP_PROFILE=.build/out/cmp.json TABLE_PROFILE=.build/out/tables.json SQLGLOT_ROOT=.build/sqlglot \
        python3 tools/compact_index.py .build/out/cov.json index.json.gz .build/out/cpu.json .build/out/core.json

## Dev commands

    python benchmark.py [--static]     # native sample score + per-commit excess (BR_VERBOSE=1 for reasons)
    python test_selector.py            # unit + sample-safety checks
    python tools/edge_cases.py         # hand-made unusual patches through the static analysis
    python tools/fidelity.py [ids]     # verifier vs. labeled failures
    python tools/mutants.py N out.json # synthetic-mutant calibration (pinned repo only)
    python tools/table_mutants.py N out.json [workers]  # class-table narrowing soundness (pinned repo only)

Run dev tools with the venv python: the system python lacks duckdb, so the verifier reports
tests/test_optimizer.py as unimportable there (harmless over-selection, but it skews CPU).

## Soundness work in v6

- Class-table profile: every class-body dict literal is wrapped during a pinned-suite run and each
  test's key reads / presence checks / enumeration / size / bulk-value / mutation are recorded.
  An edit to some entries selects only the tests that read those keys, unless the table's values
  were read in bulk or it was mutated anywhere, it was enumerated while tests ran (how a table is
  copied into a lazy per-class cache, e.g. generator TRANSFORMS -> _DISPATCH_CACHE), or a changed
  key was consulted at import. Edited values must be side-effect free. Synthetic table mutants:
  0 unsound after the enumeration rule (the earlier run found the TRANSFORMS cache case).
- Coverage now also records PY_START: a body that starts on its own definition line (a lambda in
  TRANSFORMS, `def f(): return x`) never produced a LINE event, so such edits selected nothing.
- Cache-fill rule: code that only runs on a miss of a module-level cache (_build_dispatch) also
  selects every test running the cache's owner (Generator.__init__): 15 -> 1347 tests.
- Verifier evidence: a FILE import failure lists the whole test file; a fixture (setUpClass) error
  lists the whole class; every IMPORT failure's dependents are listed, not just the first; an
  over-budget plan still runs its import probe.

## Remaining excess (2.7 s): 5y72 2.47 s; CPU (~2.4 s local, x2.2 on the board) is now the larger cost.
