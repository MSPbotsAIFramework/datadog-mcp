# PRD-21259 · Code review

## Round 1 · 2026-11-03 · reviewed dev/PRD-21259 @ 204ab0f (base 0ab3ff3)

Verdict: changes requested

### Findings

1. [must-fix] `src/datadog_mcp/api_client.py:29-33,90-117` — Worst-case retry/backoff timing can exceed the SOP's mandatory `<120s` total-call-duration ceiling (`vendor-mcp-development-sop.md` §5, a "必须" requirement: "单次工具调用总耗时（含重试）必须 < 120s，超出会被上游判超时"). With `_MAX_RETRIES=3` and `_READ_TIMEOUT=30.0`, the retry loop makes up to 4 attempts; if each attempt consumes the full 30s read timeout (`httpx.TimeoutException` path, lines 93-98), the three backoff sleeps between attempts (`0.5 + 1 + 2 = 3.5s`, line 97) are added on top: `4*30 + 3.5 = 123.5s`, already over the 120s ceiling before any `Retry-After`-driven delay (lines 107-110, 120-127) is counted. Nothing in the loop tracks or enforces a cumulative deadline across attempts. Fix: reduce `_READ_TIMEOUT` and/or `_MAX_RETRIES`, or track elapsed time across the loop and stop retrying once the remaining budget can't fit another attempt, so the sum can never reach 120s.
2. [must-fix] `src/datadog_mcp/server.py`, `src/datadog_mcp/api_client.py`, `src/datadog_mcp/tools/*.py` — No structured logging/observability exists anywhere in the service (`grep -rn "logging\|logger" src/` returns nothing), even though the frozen tech design's "Non-functional" section explicitly commits to it: `docs/tickets/PRD-21259.md:176` — "Observability: structured logs (tool name, request id, upstream path + status code, latency, error `code`) per SOP §6; never log header values or response bodies verbatim." This is also one of SOP §14's delivery checklist items (`vendor-mcp-development-sop.md:484`, "结构化日志带 request-id"), which `legs/review.md` 4.3 lists as a must-fix criterion on its own ("SOP §14 Checklist"). The gap is not disclosed anywhere as a known deviation — Self-test's "Changed files" section (`docs/tickets/PRD-21259.md:284-286`) states "every file there was created exactly as planned, no deviation." Without any request-id/latency/status logging, a multi-tenant, credential-handling service has no way to trace a failing tenant's request once it's running in production. Fix: add minimal structured logging (tool name, request id, upstream path/status code, latency, error `code`; never credential header values or raw response bodies) per SOP §6, or — if deliberately deferred — disclose it with justification in Self-test/Handoff instead of claiming it was done.

### Suggestions

3. [suggestion] `docs/tickets/PRD-21259.ac.json:135-149` — TC20's `"ac"` field is `"AC1"`, but the prose description (`docs/tickets/PRD-21259.md:238`, "TC20 (AC1–AC11)") says it exercises all 11 ACs end-to-end. Not functionally significant (the case is correctly marked `untested`/`needs a real vendor account` either way), but the ledger's per-AC coverage table understates what TC20 is meant to check.

### What was verified and passed

- `uv sync && uv run pytest -q` reproduced independently on the reviewed sha: 22 passed, 0 failed (matches the Self-test table).
- Chinese-character scan (`grep -rnP '[\x{4e00}-\x{9fff}]' --exclude-dir=.git --exclude-dir=.venv .`) reproduced: no output.
- Secret-scan heuristic reproduced: same 2 hits, both the `contextvars.Token` false positive noted in Self-test; `gitleaks` confirmed not installed in this environment either.
- README "MSPbots Connector" section diffed byte-for-byte against `vendor-mcp-development/reference/readme-connector-section.md`: identical.
- Credential handling (headers-only, `ContextVar` + `finally: reset`, fresh `DatadogClient`/`httpx.AsyncClient` per request, no module-level credential state), error envelope shape/vocabulary, tool naming (`datadog_` prefix), pagination caps (`limit`/`per_page` capped, `has_more`), stateless transport (`stateless_http=True`), DNS-rebinding disabled with documented reason, lifespan wiring to the outer Starlette app, and the `tools/list` snapshot test all match the tech design and SOP §1/§2/§3/§4.
- Multi-tenant isolation (AC9) is exercised by two independent tests (HTTP round-trip + direct `ContextVar` interleaving test) and is correct by construction (no shared mutable state between requests).
- Test coverage maps 1:1 to the AC table; no test appears to assert on implementation details rather than behavior.

### Fixes (round 1) · a6e4931

Only the two must-fix items below were touched; nothing else in the diff. Suggestion 3 (TC20 `ac` field) was left as-is -- it is a test-ledger labeling nit, not in scope for this rework round.

1. **Must-fix 1 (retry budget)** -- `src/datadog_mcp/api_client.py`: `request()` now tracks a cumulative wall-clock deadline (`_MAX_TOTAL_SECONDS = 100.0`, new module constant just above `_BACKOFF_BASE_SECONDS`). Before every attempt and before every backoff/retry sleep it recomputes `remaining = _MAX_TOTAL_SECONDS - elapsed`; if `remaining <= 0` it raises `UpstreamError(599, ...)` immediately instead of making another attempt. The network call itself is wrapped in `asyncio.wait_for(..., timeout=remaining)` so a hung upstream is cancelled once the budget runs out rather than consuming a full `_READ_TIMEOUT` regardless of how little budget is left. `_READ_TIMEOUT`/`_MAX_RETRIES` were left unchanged (still 30.0 / 3) -- the fix is the deadline check itself, not different constants, so the 120s ceiling holds even if those two are retuned later. Worst case is now bounded by `_MAX_TOTAL_SECONDS` (100s) plus negligible scheduling overhead, comfortably under the SOP's 120s ceiling.
   - New regression test `test_retry_loop_stops_once_time_budget_is_exhausted` (`tests/test_tools.py`): a transport that sleeps 10s on every call (would hang well past 120s under the old code, which had no cancellation mechanism at all) is retried against a monkeypatched `_MAX_TOTAL_SECONDS = 0.3`; asserts the call raises `UpstreamError(599, ...)` and completes in under 2s.
2. **Must-fix 2 (structured logging)** -- new module `src/datadog_mcp/logging_utils.py`: a dedicated `datadog_mcp` logger with a JSON-lines formatter (`configure_logging()`, called once from `server.build_app()`, idempotent), a `ContextVar`-based request id (`new_request_id()` / `set_request_id()` / `reset_request_id()` / `current_request_id()`), and `log_upstream_call(tool, method, path, status_code, latency_ms, error_code)`.
   - `server.py`: `CredentialsMiddleware` generates one request id per HTTP request (`new_request_id()`) and sets/resets it via the `ContextVar` in the same `try/finally` that already resets the credentials token -- never derived from header values.
   - `server.py`: `client_factory` now takes `tool_name: str` and passes it through to `DatadogClient(..., tool_name=...)`.
   - `api_client.py`: `DatadogClient.__init__` accepts a keyword-only `tool_name: str = ""` (default keeps the two pre-existing tests that construct it directly, with no `tool_name`, working unchanged). `request()` logs exactly once per call, in a `finally` block covering the whole retry loop, with the tool name, current request id, HTTP method, upstream path, final status code (`None` on timeout/network error), total latency in ms, and the error code mapped via `errors.from_status_code(...).code` (same fixed vocabulary the tools already return to callers) -- never the raw exception message, header values, or response body.
   - `tools/monitors.py` (4 call sites), `tools/logs.py`, `tools/metrics.py` (2), `tools/events.py`: each of the 8 `client_factory()` call sites now passes its own tool name, e.g. `client_factory("datadog_get_monitor")`.
   - New regression tests in `tests/test_tools.py`: `test_upstream_call_emits_one_structured_log_line_without_credentials` asserts a successful call logs exactly one record with the right `tool`/`request_id`/`path`/`status_code`/`error_code`/`latency_ms` fields and that the rendered message never contains the (deliberately distinctive) credential values used in the test; `test_upstream_call_log_line_records_mapped_error_code_on_failure` asserts a 404 response logs `status_code=404, error_code="not_found"`. A standalone manual check (not committed) confirmed the formatter emits valid, parseable JSON with exactly the documented field set.

**Retest on a6e4931** (synced sha unchanged, default branch `main` still at `0ab3ff3` -- fetched and confirmed not moved, no re-merge needed):
- `uv sync && uv run pytest -q`: **25 passed, 0 failed** (22 from round 1 + 3 new: `test_retry_loop_stops_once_time_budget_is_exhausted`, `test_upstream_call_emits_one_structured_log_line_without_credentials`, `test_upstream_call_log_line_records_mapped_error_code_on_failure`).
- Chinese-character scan (`grep -rnP '[\x{4e00}-\x{9fff}]' --exclude-dir=.git --exclude-dir=.venv .`): 2 hits, both in `docs/tickets/PRD-21259-review.md` lines 9-10 -- pre-existing in this file at the reviewed sha `5364a29` (verified via `git show 5364a29:docs/tickets/PRD-21259-review.md`, same 2 hits there), quoting the Chinese-language SOP text as finding evidence; not part of this round's diff and not new. `src/`, `tests/`, and `README.md` remain clean.
- Secret-scan heuristic: unchanged from round 1 (2 hits, both the known `contextvars.Token` false positive); `gitleaks` still not installed in this environment.
- README "MSPbots Connector" section: unchanged by this round (not in the diff), still byte-identical to `vendor-mcp-development/reference/readme-connector-section.md` as verified in round 1.
- Smoke: `GET /health` -> 200 `{"status": "ok"}`; `POST /mcp` with missing credential headers -> 401 with the same error envelope as round 1 (unchanged code path). Full `tools/list` contract already covered by the unchanged `test_tools_list_snapshot` test, which passed.

Not changed: `docs/tickets/PRD-21259.md` (frozen main doc) is untouched per the review-rework rule -- this Fixes section is the record of what changed.

## Round 2 · 2026-11-03 · reviewed dev/PRD-21259 @ a927da1 (base main @ 0ab3ff3, unmoved since round 1)

Verdict: approved

### Scope

`git diff 5364a29...a927da1` (the round 1 fix commit `a6e4931` plus the round 1 Fixes-section doc commit `a927da1`) -- 9 files, +321/-42: `src/datadog_mcp/api_client.py`, new `src/datadog_mcp/logging_utils.py`, `src/datadog_mcp/server.py`, all 8 `client_factory()` call sites across `tools/{events,logs,metrics,monitors}.py`, `tests/test_tools.py`, and this review doc. No touch to the frozen `docs/tickets/PRD-21259.md`, `PRD-21259.ac.json`, or `README.md` -- correct, this round is a pure fix of the two round 1 must-fix items, not a scope or contract change.

### Verification of round 1 fixes

1. **Must-fix 1 (retry budget).** `api_client.py` now tracks a cumulative wall-clock deadline (`_MAX_TOTAL_SECONDS = 100.0`): before every attempt and before every backoff/retry sleep it recomputes `remaining` and raises `UpstreamError(599, ...)` immediately once `remaining <= 0`, and wraps the network call itself in `asyncio.wait_for(..., timeout=remaining)` so a hung upstream is cancelled rather than consuming a full 30s read timeout regardless of budget left. Traced every exit path (success, timeout/`asyncio.TimeoutError`, retryable status, non-retryable status, retries exhausted) -- all either return/raise before the 100s ceiling or raise the new `599` budget-exhausted error, so the worst case is bounded at ~100s + negligible scheduling overhead, comfortably under the SOP's 120s per-call ceiling (vendor-mcp-development-sop.md §5). New regression test `test_retry_loop_stops_once_time_budget_is_exhausted` reproduces independently: monkeypatches `_MAX_TOTAL_SECONDS = 0.3` against a transport that sleeps 10s on every call, asserts `UpstreamError(599)` and `elapsed < 2.0`. **Confirmed fixed.**
2. **Must-fix 2 (structured logging).** New `logging_utils.py` (`configure_logging`, request-id `ContextVar`, `log_upstream_call`) is wired from `server.build_app()` (idempotent `configure_logging()` call) and `CredentialsMiddleware` (one `request_id` per HTTP request, set/reset in the same `try/finally` that already resets the credentials `ContextVar` -- never derived from header values). `api_client.DatadogClient.request()` logs exactly once per call, in a `finally` covering the whole retry loop, with tool name, request id, method, path, status code, latency (ms), and the already-classified error `code` -- never the raw exception message, header values, or response body. All 8 `client_factory()` call sites (across `events.py`, `logs.py`, `metrics.py` x2, `monitors.py` x4) now pass their own tool name; checked each one individually, none missed. Two new regression tests confirm: one asserts a successful call logs exactly one record with the right fields and that the rendered message never contains the test's (deliberately distinctive) credential values; the other asserts a 404 logs `status_code=404, error_code="not_found"`. **Confirmed fixed.**

### What was verified and passed (reproduced independently on a927da1)

- `uv sync && uv run pytest -q`: **25 passed, 0 failed** (22 from round 1 + the 3 new tests above).
- CJK scan (`grep -rnP '[\x{4e00}-\x{9fff}]' --exclude-dir=.git --exclude-dir=.venv .`): only the 2 pre-existing hits in this review doc itself (quoting the Chinese-language SOP text as round 1 finding evidence) -- same 2 hits as at round 1's reviewed sha `5364a29` (`git show 5364a29:docs/tickets/PRD-21259-review.md`), not introduced by this round. `src/`, `tests/`, `README.md` clean.
- `src/datadog_mcp/errors.py` has both `UPSTREAM_ERROR` and `from_status_code` that `api_client.py` newly imports -- no missing-symbol risk.
- Credential values (`"secret-key"`, `"secret-app-key"`) used in the new logging test are asserted absent from the rendered log message -- the new logging path does not leak credentials, consistent with SOP §3/§6.

### Suggestions carried over (unchanged, still not blocking)

1. `docs/tickets/PRD-21259.ac.json:135-149` -- TC20's `"ac"` field says `"AC1"` but the prose says it covers AC1-AC11 (round 1 suggestion 3). Left as-is this round; still not functionally significant.

### Findings

None. Both round 1 must-fix items are confirmed fixed; no new must-fix found in this round's diff.

Status: frozen @ a927da1
