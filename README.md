# ga4-mcp

> **Production-grade MCP server for Google Analytics 4 — self-hosted, token-secured, Claude-connector-ready.**

A Model Context Protocol server that gives Claude, ChatGPT, Cursor, and any other MCP client direct access to your GA4 data — property metadata, custom dimensions and metrics, run_report (the full Data API), real-time reporting, property annotations, and Google Ads link inventories.

Forked from [googleanalytics/google-analytics-mcp](https://github.com/googleanalytics/google-analytics-mcp) (Google's official) and hardened for remote deployment behind a reverse proxy. Adds streamable-HTTP transport, token-gated auth, CORS, the path-rewrite fix that makes the Claude web connector actually load, and explicit `prompts`/`resources` capability declarations.

## Why this one over the upstream

| | Upstream (Google) | This fork |
|---|---|---|
| Stdio transport for local Claude Desktop | ✅ | ✅ |
| Basic Bearer auth on SSE | ✅ | ✅ |
| **Streamable-HTTP transport at `/mcp`** | ❌ | ✅ |
| **Bare `/mcp` works (no 307 → http:// redirect)** | ❌ | ✅ |
| **Token auth via 4 paths** (`?token=`, Bearer, X-Api-Key, Mcp-Auth-Token) | partial | ✅ |
| **CORS for browser-based MCP clients** | ❌ | ✅ |
| **No-compress middleware** (Claude web connector breaks on brotli) | ❌ | ✅ |
| **`prompts` + `resources` capabilities declared in initialize** *(see below)* | ❌ | ✅ |
| **`/healthz` endpoint** | ❌ | ✅ |
| **`.env` loading** via `python-dotenv` | ❌ | ✅ |
| **`proxy_headers=True`** (X-Forwarded-Proto respected) | ❌ | ✅ |
| **Pure-ASGI path rewriter** (bare `/mcp` → `/mcp/` before routing) | ❌ | ✅ |

### The non-obvious fixes you don't find on Google

1. **Bare `/mcp` returned 307 to `http://`.** Starlette's `Mount("/mcp", X)` regex requires a trailing slash, so a bare POST `/mcp` falls through to a 307 redirect. Worse, Traefik terminating TLS doesn't always rewrite the Location header back to `https://` — so Claude's connector validator got an `http://` redirect and rejected it with "Couldn't reach the MCP server". Fix: pure-ASGI middleware rewrites `scope["path"]` from `/mcp` to `/mcp/` *before* the router sees it. Also `proxy_headers=True` on uvicorn so any future redirect stays HTTPS.

2. **Missing capability blocks in initialize.** Claude's connector validator rejects servers whose initialize response omits the `prompts` or `resources` capability — even when the server has nothing to offer in those slots. FastMCP-based servers escape this because their wrapper registers default handlers, but a bare `mcp.server.lowlevel.Server` only declares a capability when at least one handler is registered for it. Fix: register empty `list_prompts` / `list_resources` / `list_resource_templates` handlers so the initialize response declares all four blocks (`experimental`, `prompts`, `resources`, `tools`).

If you've ever spent an afternoon debugging "Couldn't reach the MCP server" with a working curl, these are the two fixes you needed.

## Tool surface — 7 tools

`get_account_summaries` · `list_google_ads_links` · `get_property_details` · `list_property_annotations` · `get_custom_dimensions_and_metrics` · `run_report` · `run_realtime_report`

All from the upstream — this fork hardens infrastructure, doesn't change tool behavior.

## Quick start — local (Claude Desktop)

```bash
git clone https://github.com/thesyedyahya/ga4-mcp.git
cd ga4-mcp
uv venv .venv
uv pip install -e .
export GOOGLE_APPLICATION_CREDENTIALS=/abs/path/to/service-account.json
analytics-mcp
```

In `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ga4": {
      "command": "/abs/path/to/.venv/bin/analytics-mcp",
      "env": {
        "GOOGLE_APPLICATION_CREDENTIALS": "/abs/path/to/service-account.json"
      }
    }
  }
}
```

## Quick start — remote (Docker / Dokploy / any VPS)

```bash
docker build -t ga4-mcp .
docker run -d -p 8080:8080 \
  -e GOOGLE_APPLICATION_CREDENTIALS_JSON="$(cat service-account.json | python3 -c 'import json,sys;print(json.dumps(json.load(sys.stdin)))')" \
  -e MCP_AUTH_TOKEN="$(openssl rand -hex 32)" \
  -e MCP_ALLOWED_ORIGINS="*" \
  ga4-mcp
```

Put Caddy/Traefik in front for HTTPS, point a domain at it, then add the connector in Claude:

```
https://mcp-ga4.example.com/mcp?token=<your-token>
```

## Auth setup

The service account must be added as a **Viewer** (or higher) on each GA4 property you want to query — not just at the account level. If `get_account_summaries` returns 0 properties, the SA is on the account but not on any properties.

1. GCP Console → IAM & Admin → Service Accounts → Create + JSON key
2. [Enable the Google Analytics Data API](https://console.cloud.google.com/apis/library/analyticsdata.googleapis.com)
3. Google Analytics → Admin → **Property access management** → Add user → enter the SA email → Viewer
4. Repeat for each property the MCP should access
5. Set `GOOGLE_APPLICATION_CREDENTIALS` (local) or `GOOGLE_APPLICATION_CREDENTIALS_JSON` (Docker)

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GOOGLE_APPLICATION_CREDENTIALS` | local | — | Absolute path to SA JSON |
| `GOOGLE_APPLICATION_CREDENTIALS_JSON` | container | — | Single-line SA JSON. Entrypoint decodes to a file. |
| `MCP_TRANSPORT` | No | `stdio` (local), `streamable-http` (Docker) | `stdio` / `sse` / `streamable-http` |
| `MCP_HOST` | No | `0.0.0.0` (Docker) | Bind host |
| `MCP_PORT` | No | `8080` | Bind port |
| `MCP_AUTH_TOKEN` | Public deploy | — | If unset, server runs open |
| `MCP_ALLOWED_ORIGINS` | No | `*` | CORS allowlist (token gates access) |

## Sample prompts

After connecting:

- "List my GA4 properties and their key timezones."
- "Run a 28-day report on property `properties/XXXXX` — sessions, conversions, average engagement time, broken down by source/medium."
- "Show me the last hour of users on property `properties/XXXXX`, broken down by country."
- "List custom dimensions and metrics on `properties/XXXXX`. Which are unused in the last 30 days?"

## License

Apache 2.0. See [LICENSE](LICENSE). Includes substantial portions from [googleanalytics/google-analytics-mcp](https://github.com/googleanalytics/google-analytics-mcp) — credit + thanks to the GA team (Josh Radcliff, Matt Landers, and contributors).

## About

Maintained by [@thesyedyahya](https://github.com/thesyedyahya) — I build production MCP servers for Claude, ChatGPT, and Cursor. If you need a custom MCP for your API or help deploying one, [open an issue](https://github.com/thesyedyahya/ga4-mcp/issues) or reach me on Upwork or Fiverr.
