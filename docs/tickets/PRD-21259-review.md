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
