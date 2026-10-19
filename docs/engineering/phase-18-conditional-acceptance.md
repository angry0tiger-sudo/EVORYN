# Phase 18 — Conditional Acceptance and Infrastructure Blocker

- Date: 2026-10-19
- PR: [#15 — Connectivity Manager](https://github.com/angry0tiger-sudo/EVORYN/pull/15)
- Scope: Conditional progression authorization only; not a declaration that every release gate passed.
- Decision authority: Project owner explicitly authorized a conditional merge while the hosted CI runner allocation blocker remains unresolved.

## Executive status

Phase 18's implementation is present on the feature branch and received narrow, reproducible local checks. GitHub Actions jobs on the latest observed candidate were created but **never started**: the jobs API reports `runner_id=0`, empty `runner_name`, and `steps=[]` for all six jobs. They failed approximately 2–3 seconds after creation. This is direct evidence of a runner-allocation failure; it is not evidence that the source passed or failed those commands.

The owner has directed the project to proceed under a documented conditional waiver rather than wait indefinitely for hosted-runner allocation. The merge, if accepted by GitHub, records a risk-accepted progression decision. It must not be interpreted as CI-green, independently reviewed, or production-ready status.

## Local evidence available

The following results were captured in the isolated local validation harness and recorded in the supplementary evidence report:

1. `python -m compileall -q src tests` — passed, exit code 0, for the isolated source snapshot.
2. `PYTHONPATH=src python -m pytest -vv tests/unit/test_phase18_local_smoke.py` — passed, 9 tests in 0.04 seconds. Coverage includes Retry-After parsing, destination rejection, bounded transient retry, non-retryable authentication errors, rate-limit cooldown, circuit recovery, audit sink failure, stale/future/duplicate/gap rejection, and expired route authorization.
3. The local SHA-1 for `src/evoryn/adapters/network/json_http.py` matched the GitHub blob SHA `7eb3ee44c8aec7cbefc77e58de6a4a8b66a190a2`.
4. The local SHA-1 for `src/evoryn/adapters/network/connectivity.py` matched the GitHub blob SHA `2c2c2affa51b34edc259a523c449a6b19cfd798c`.
5. An isolated editable packaging sanity check passed with `--no-deps --no-build-isolation` and a minimal local README placeholder. This is not equivalent to the canonical CI dependency installation.
6. The full canonical test suite was not executed locally. Ruff, mypy, Bandit and pip-audit were not run locally because their executables were unavailable. This is explicitly not a full-project green result.

The complete scope, commands, hashes and limitations are preserved in the local supplementary evidence report attached to the working conversation as `EVORYN_Phase18_Local_Smoke_Evidence.txt`. That report has not been represented as a full repository checkout.

## GitHub Actions evidence

Observed runs on the Phase 18 branch include:

- Run #595, ID `39212965648`, push event, candidate SHA `e21d27271b0fd6e279c09bd056f06bc74d2a88d9`.
- Run #596, ID `39212970490`, pull-request event, same SHA.
- Earlier diagnostic run #594, ID `39211657505`; one controlled re-run reproduced the same failure mode.

For each of the six jobs in runs #595 and #596:
- conclusion: `failure`;
- runner ID: `0`;
- runner name: empty;
- steps: empty;
- runtime: approximately 2–3 seconds.

Log/artifact visibility also failed (`BlobNotFound`, no diagnostic artifacts). A root cause at the *job-execution* level is confirmed: no runner was assigned and the commands did not execute. The lower-level reason for missing runner assignment is not available in the exposed evidence. Private-repository billing/minute allowance or Actions settings are possible owner-side checks, not facts established by the API.

## Conditional merge semantics

By explicit owner instruction, Phase 18 may be merged as a conditional progression step with a commit message that records the known CI limitation. This authorization waives waiting for CI execution; it does not waive truthfulness or remove the following gates.

**Not certified by this conditional acceptance:**
- full canonical pytest suite passing;
- Ruff passing;
- strict mypy passing;
- Bandit passing;
- pip-audit passing;
- independent code/risk review;
- durable audit-sink and route-authorization integration in the intended runtime;
- production readiness or authority to use exchange write endpoints.

No exchange write path, live-order routing, hard-risk-limit mutation or Auto-Halt bypass was introduced. No real exchange order was sent.

## Mandatory technical-debt follow-up

The exact outstanding checks are tracked in the repository root `TECHNICAL_DEBT.md`. They must be re-run against the merged main commit when a trusted runner or equivalent full checkout becomes available. Record command, environment, commit SHA, exit code, and full output/artifact reference. Do not mark a gate complete from the presence of a workflow file or a prior smoke test.

## Phase 19 progression boundary

Phase 19 may begin only as **read-only, observational SHADOW**. It may consume an approved market-data feed and produce hypothetical decision intents for logging. It must not submit orders, route writes to an exchange, amend hard risk limits, or relabel SHADOW evidence as PAPER. A conditional Phase 18 merge is not a live-trading authorization. All new Phase 19 gates remain explicitly documented in its ADR and operations guide.
