# datadog-mcp

A Model Context Protocol (MCP) server that gives AI agents live access to
[Datadog](https://www.datadoghq.com/)'s monitors, logs, metrics, and events
through the MSPbots Agent platform.

- **Transport**: MCP Streamable HTTP (`POST /mcp`), stateless, no session
  affinity required.
- **Tech stack**: Python 3.12, [FastMCP](https://github.com/modelcontextprotocol/python-sdk),
  `httpx`.
- **License**: Apache-2.0. Copyright MSPbots.ai.

---

<div align="center">

## MSPbots Connector

**The MCP Gateway built for MSPs: visible, controlled, and your data stays where it belongs.**

[**Get started at connect.mspbots.ai →**](https://connect.mspbots.ai)

</div>

---

MSPbots Connector gives your AI clients one MCP URL to reach your entire MSP stack (RMM, PSA, documentation, security, finance), and gives you control over who can call which tool and a record of everything that was called.

**Hosted service: [connect.mspbots.ai](https://connect.mspbots.ai)** · Free to use.

### Why Connector

![MSPbots Connector](https://stgoss.mspbots.ai/files/mcp/mspbots-mcp.png)

| | |
|---|---|
| **Visible** | Every tool call is logged: who, which vendor, when. Usage dashboard, CSV export, SIEM streaming. |
| **Controlled** | Role-based, per-tool allowlist. Deny by default. |
| **Protected** | Credentials encrypted per record. AI clients never see your vendor passwords or tokens. |
| **MSP-native** | Built for the MSP stack only: RMM, PSA, documentation, security, finance. |

### Quick start

1. **Sign up** at [connect.mspbots.ai](https://connect.mspbots.ai).
2. **Connect your vendors.** Pick from 150+ connectors and authorize each one on the vendor's own page, or bring your own API keys. No extra vendor accounts needed.
3. **Set access.** Create roles and decide which tools each role may call. Nothing is allowed until you allow it.
4. **Add your MCP URL to your AI client.** Copy the URL from your Connector dashboard into Claude, ChatGPT, Cursor, VS Code, Gemini, Codex, or any MCP-capable client.
5. **Ask your AI to do real work**, and watch every call appear in the audit log.

### Features

**Unified gateway**
- One MCP URL for all your AI clients
- 150+ connectors across the MSP stack
- Custom MCP servers supported, free
- Authorization happens on each vendor's own page; the AI never sees passwords or tokens
- Bring your own credentials: use the vendor API keys you already have
- Multiple accounts per app; connect once, reuse across AI clients
- Tool search: finds the right action for the task
- One sign-in; vendor credentials are injected behind the scenes and OAuth tokens refresh automatically

**Visibility**
- Audit log for every tool call (who, which vendor, when)
- Usage dashboard: call volume, top vendors, error rate, per-user activity
- Export audit logs to CSV, or stream to Loki, Graylog, or LogScale (SIEM)
- Optional recording of tool arguments and result summaries (off by default, enabled per organization)

**Control**
- Per-role, per-tool allowlist, deny by default
- owner / admin / member roles
- SCIM provisioning with Entra ID, Okta, JumpCloud, and Google Workspace

**Security**
- Credentials encrypted one record at a time (AES-256-GCM, per-record key)
- AI clients authenticate with OAuth 2.1 + PKCE
- Vendor responses are scanned for prompt-injection patterns and returned as structured data. This is one layer of defense, not a guarantee against every attack.
- Static egress IP for customer firewall and API allowlisting

### Pricing

**Free.**

Links: [connect.mspbots.ai](https://connect.mspbots.ai) · [mspbots.ai](https://mspbots.ai)

---

## Authentication headers

Every request to `POST /mcp` must carry these headers. Credentials are read
per-request only -- never from environment variables or a config file, and
never persisted or logged.

| Header | Required | Meaning | Where to get it |
|---|---|---|---|
| `X-Datadog-Api-Key` | Yes | Datadog API key | Datadog → Organization Settings → API Keys |
| `X-Datadog-Application-Key` | Yes | Datadog Application key | Datadog → Organization Settings → Application Keys |
| `X-Datadog-Site` | No (default `datadoghq.com`) | Which Datadog site/region the account lives on | Datadog → user menu → My Preferences (shows the account's site) |

Accepted `X-Datadog-Site` values: `datadoghq.com` (default, US1), `datadoghq.eu`
(EU), `us3.datadoghq.com` (US3), `us5.datadoghq.com` (US5),
`ap1.datadoghq.com` (AP1), `ddog-gov.com` (US1-FED). An unrecognized value
returns a structured `invalid_argument` error listing the accepted values --
it never silently falls back to the US1 default.

Both the API key and Application key are required on every tool call, even
on the subset of Datadog's own endpoints that technically only need one --
this keeps the 401 behavior and this README's header table the single thing
an integrator has to get right, instead of a per-tool matrix.

## Tools

All tools are read-only except the two monitor mute/unmute tools, which are
reversible and scoped to a single named monitor.

| Tool | Datadog API | Notes |
|---|---|---|
| `datadog_search_monitors` | `GET /api/v1/monitor/search` | Paginated (`page`, `per_page`, cap 100). |
| `datadog_get_monitor` | `GET /api/v1/monitor/{id}` | Full monitor detail by id. |
| `datadog_mute_monitor` | `POST /api/v1/monitor/{id}/mute` | Optional scope and expiry; not idempotent (repeated calls update the mute window). |
| `datadog_unmute_monitor` | `POST /api/v1/monitor/{id}/unmute` | Idempotent. |
| `datadog_search_logs` | `POST /api/v2/logs/events/search` | Paginated via `cursor`; empty matches are not an error. |
| `datadog_query_metrics` | `GET /api/v1/query` | Timeseries points for a metric query string. |
| `datadog_list_active_metrics` | `GET /api/v1/metrics` | Truncated client-side (no native page size); optional `prefix` filter. |
| `datadog_search_events` | `POST /api/v2/events/search` | Paginated via `cursor`; empty matches are not an error. |

Dashboards, Incidents, APM, Synthetics, SLOs, RUM, Security Monitoring, and
Cloud Cost are separate Datadog product surfaces and are not covered in this
first version; each would be a new `tools/<resource>.py` file added to this
same repository if a concrete need appears.

## Error format

Every tool returns a JSON string. Failures use a structured envelope instead
of raising an exception:

```json
{"error": {"code": "not_found", "message": "Monitor 999999999 does not exist.", "retryable": false}}
```

`code` is one of `not_configured`, `unauthorized`, `not_found`,
`invalid_argument`, `rate_limited`, `upstream_error`. `retryable` tells the
caller whether a retry might succeed (`rate_limited`, `upstream_error`
only).

## Local development

```bash
uv sync
uv run pytest
uv run python -m datadog_mcp   # listens on 0.0.0.0:8080
```

With the service running, exercise it with real (or placeholder-shaped) test
credentials -- this is the same request shape the Gateway uses in
production, just with your own headers instead of a tenant's:

```bash
curl -X POST http://localhost:8080/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'X-Datadog-Api-Key: <your key>' \
  -H 'X-Datadog-Application-Key: <your app key>' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

```bash
docker build --platform linux/amd64 -t datadog-mcp:dev .
docker compose up --build
```
