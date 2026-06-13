# Spike: Standalone Gmail & Outlook MCP servers (read-only) — 2026-06-10

**Task 0.2 (research portion).** Goal: pick one self-hostable, standalone MCP *server* for
Gmail and one for Outlook/Microsoft-Graph mail that our Flask app's embedded MCP *client*
can launch over stdio to fetch a tiny, **read-only** email sample for work/personal
classification. No live OAuth performed in this spike (deferred to when the user can auth
with their real account). This is research + documentation only.

What we need from a server, in priority order:
1. **Read-only capable** — can request only `gmail.readonly` / Graph `Mail.Read` and NOT
   expose write/send/delete tools.
2. **Active maintenance** + permissive license (MIT/Apache).
3. **stdio transport** (so the client can spawn it as a subprocess; no long-running HTTP
   service to manage inside the app).
4. **Its own local OAuth** + an exposable **search tool** with a knowable signature.
5. **Minimal setup** — ideally no per-user cloud app registration.

---

## Candidate table — Gmail

| Repo | Maint. / License | Lang/Runtime | Maturity | OAuth flow & token store | Read-only? | Transport | Search tool |
|------|------------------|--------------|----------|--------------------------|-----------|-----------|-------------|
| **taylorwilsdon/google_workspace_mcp** | taylorwilsdon / **MIT** | **Python 3.10+** (`uvx workspace-mcp` / `pip install workspace-mcp`) | **2.6k★, 87 releases, very active** | Loopback browser OAuth; tokens at `~/.google_workspace_mcp/credentials/` (plaintext JSON). **User must supply own Google Cloud OAuth client** (`GOOGLE_OAUTH_CLIENT_ID/SECRET`). | **Yes — `--read-only` flag** requests only read scopes AND disables write tools. | **stdio** (also streamable-http) | `search_gmail_messages` |
| GongRzhe/Gmail-MCP-Server | GongRzhe / MIT | TS/JS (`npx @gongrzhe/server-gmail-autoauth-mcp`) | 1.1k★ but **ARCHIVED 2026-03-03 (read-only repo, unmaintained)** | Loopback browser OAuth; `~/.gmail-mcp/credentials.json`. Requires own GCP `gcp-oauth.keys.json`. | No explicit read-only mode; full-scope, exposes send/modify tools. | stdio | `search_emails` |
| Maheidem/gmail-mcp | Maheidem / MIT | Python (`uvx mcp-gmail-reader`) | **0★, 5 commits, no releases** (immature) | Browser OAuth; `~/.gmail-mcp/token.json` + `gcp-oauth.keys.json`. Requires own GCP client. | **Yes — `gmail.readonly` only, by design** ("cannot send or modify"). | stdio | `search_emails` (returns IDs → `get_email`) |
| navbuildz/gmail-mcp-server | navbuildz / open-source | TS, Docker | active | OAuth2 | No — multi-account read **+ write/archive/label/unsubscribe** | stdio/HTTP | — |

## Candidate table — Outlook / Microsoft Graph mail

| Repo | Maint. / License | Lang/Runtime | Maturity | OAuth flow & token store | Read-only? | Transport | Mail tools |
|------|------------------|--------------|----------|--------------------------|-----------|-----------|-----------|
| **Softeria/ms-365-mcp-server** | Softeria / **MIT** | **Node.js ≥20** (`npx @softeria/ms-365-mcp-server`) | **774★, 241 releases, very active** | **Device-code flow** (default; no client secret). **Built-in shared Azure app — NO per-user app registration required for personal accounts.** Tokens in OS credential store (keytar) w/ file fallback. | **Yes — `--read-only` flag** disables writes; `--preset mail` and `--enabled-tools '^(list-mail\|get-mail)'` restrict surface. | **stdio** (also HTTP/streamable) | `list-mail-messages`, `get-mail-message` |
| ryaker/outlook-mcp | ryaker / — | Node | moderate | OAuth2; env client id/secret/tenant | No explicit RO; exposes send/organize | stdio | list/search/read/send |
| nsakki55/outlook-mcp | nsakki55 / — | — | small | **Auth Code + PKCE, no client secret** | partial | — | — |
| mcp-z/mcp-outlook | mcp-z / — | TS | moderate | OAuth2 (Mail.ReadWrite, Mail.Send default) | write-capable by default | stdio/http | search/batch |
| mpalermiti/outlook-mcp | mpalermiti / — | — | personal-acct focused | OAuth2 | 54 tools incl. write | — | mail/cal/contacts/... |

---

## Recommendation

### Gmail → **taylorwilsdon/google_workspace_mcp**

Single strongest reason: it's the only mature, actively-maintained option with a **first-class
`--read-only` flag that both requests read-only scopes and removes write tools** — exactly the
constraint guardrail this project needs. It is Python (matches our Flask/Python stack and the
Python MCP SDK client), MIT, runs over stdio via `uvx workspace-mcp`, and exposes a clean
`search_gmail_messages` tool.

- Run (read-only, Gmail only, stdio):
  `uvx workspace-mcp --tools gmail --tool-tier core --read-only`
- Why not GongRzhe: 1.1k★ but **archived/unmaintained as of 2026-03-03** and no read-only mode.
- Why not Maheidem: read-only by design, but **0★ / 5 commits** — too immature to depend on.
  (Keep as a fallback reference; its `search_emails` shape is simple.)

**Runtime to bundle:** Python 3.10+ and `uv`/`uvx` (or `pip install workspace-mcp` into our venv).
Since the app is already Python, we can `pip install workspace-mcp` into the same environment
and launch its console script via stdio — no separate runtime needed.

### Outlook → **Softeria/ms-365-mcp-server**

Single strongest reason: it ships a **built-in shared Azure app + device-code login, so a
personal Outlook/Microsoft account needs NO Azure app registration and NO client secret** — the
lowest-friction path to a working read-only Outlook sample. It also has a `--read-only` flag and
a `mail` preset, 774★ with 241 releases (very active), MIT, and runs over stdio via npx.

- Run (read-only, mail only, stdio):
  `npx @softeria/ms-365-mcp-server --read-only --preset mail`
- Login (one-time, device code): `npx @softeria/ms-365-mcp-server --login`
- Supports personal accounts by default (`--org-mode` only for work/school; tenant defaults to
  `common`).

**Runtime to bundle:** Node.js ≥20 + npx. This is the one extra runtime we must ship/require
(our app is otherwise Python). Acceptable: Node is a single, well-understood dependency, and the
device-code-no-registration UX is worth it. (If avoiding Node entirely becomes a hard
requirement later, revisit a Python Graph server — but none of the Python Outlook candidates
match Softeria on maturity or the no-registration personal-account flow.)

---

## OAuth & scope notes (what actually happens at auth time — deferred to Phase 3)

### Gmail (google_workspace_mcp)
- Flow: **loopback browser** — server opens the default browser to Google consent; if it can't,
  it returns an authorization URL to open manually, then you retry the tool call.
- Scope: with `--read-only` it requests **only `https://www.googleapis.com/auth/gmail.readonly`**
  and does not register `send_gmail_message` / `modify_gmail_message_labels`.
- Token store: `~/.google_workspace_mcp/credentials/` (plaintext JSON by default; GCS+CMEK option
  exists but irrelevant for a local app).
- Required env: `GOOGLE_OAUTH_CLIENT_ID` (required), `GOOGLE_OAUTH_CLIENT_SECRET` (omit for a
  public PKCE client), `OAUTHLIB_INSECURE_TRANSPORT=1` only for http:// loopback in dev.

### Outlook (ms-365-mcp-server)
- Flow: **device code** — server prints a URL + code; user authorizes in any browser. No client
  secret, no redirect URI to configure.
- Scope: with `--read-only` write operations are disabled; restrict to mail with `--preset mail`
  or `--enabled-tools '^(list-mail|get-mail)'`. The read mail tools map to Graph `Mail.Read`.
- Token store: OS credential store via keytar; file fallback at
  `MS365_MCP_TOKEN_CACHE_PATH` (e.g. `~/.config/ms365-mcp/.token-cache.json`).
- App registration: **not required for personal accounts** (uses built-in app id). Optional
  custom app via `MS365_MCP_CLIENT_ID` / `MS365_MCP_TENANT_ID` / `MS365_MCP_CLIENT_SECRET`.

---

## Search-tool signatures (for the capped wrapper in Phase 3)

These are the tools `CappedToolLayer` will wrap; we cap `max_results`/`top` at 5 and only ever
read.

### Gmail — `search_gmail_messages`
- **Params:** `query` (Gmail search operators, e.g. `from:alice@example.com newer_than:1y`),
  `max_results` (int).
- **Returns:** list of message stubs — id, subject, sender, snippet. Full body via
  `get_gmail_message_content(message_id)` (only call if the snippet is insufficient).
- Result content arrives as MCP content blocks (`result.content[0].text`, often JSON we parse).

### Outlook — `list-mail-messages` (+ `get-mail-message`)
- **Params:** Graph-style paging/filter options (`top`/`$top` for count, plus filter/search
  options). For "find mail from a known sender" we'll filter on sender and set `top=5`.
- **Returns:** message list with sender, subject, body (text or HTML per
  `MS365_MCP_BODY_FORMAT`). Single message via `get-mail-message`.
- Confirm exact param names against `list-tools` output at integration time (Graph wrappers vary
  by release); the wrapper should introspect the input schema rather than hard-code.

> Implementation note: don't hard-code param names. In the wrapper, call `session.list_tools()`,
> read each tool's `inputSchema`, and map our capped `top_n`/`query` onto whatever the chosen
> server's schema actually exposes. This keeps us resilient across server versions.

---

## Python MCP SDK client approach (the `mcp` PyPI package)

We launch the chosen server as a **stdio subprocess** and talk to it with `ClientSession`. This
is what `mcp_client.py` (Phase 3.2) will adapt. Install: `pip install mcp` (the official
`modelcontextprotocol/python-sdk`).

Minimal, read-only sketch (Gmail; swap `command`/`args` for the Outlook npx invocation):

```python
import asyncio
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class EmailMCPClient:
    """Spawns a standalone email MCP server over stdio and calls its read-only search tool.

    Gmail:   command="uvx",  args=["workspace-mcp","--tools","gmail","--tool-tier","core","--read-only"]
    Outlook: command="npx",  args=["@softeria/ms-365-mcp-server","--read-only","--preset","mail"]
    """

    def __init__(self, command: str, args: list[str], env: dict | None = None):
        self._params = StdioServerParameters(command=command, args=args, env=env)
        self._stack = AsyncExitStack()
        self.session: ClientSession | None = None

    async def connect(self):
        read, write = await self._stack.enter_async_context(stdio_client(self._params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        # Safety gate: assert no write/send/delete tools are exposed before we proceed.
        names = [t.name for t in (await self.session.list_tools()).tools]
        assert not any(k in n.lower() for n in names
                       for k in ("send", "delete", "draft", "modify", "trash", "reply")), \
            f"write-capable tools exposed: {names}"
        return names

    async def search(self, tool_name: str, query: str, top_n: int = 5):
        # top_n is hard-capped by CappedToolLayer; never exceed the sample size.
        result = await self.session.call_tool(tool_name, {"query": query, "max_results": top_n})
        # MCP returns content blocks; text block(s) usually carry JSON we parse.
        return [c.text for c in result.content if getattr(c, "type", None) == "text"]

    async def aclose(self):
        await self._stack.aclose()


async def _demo():
    client = EmailMCPClient(
        command="uvx",
        args=["workspace-mcp", "--tools", "gmail", "--tool-tier", "core", "--read-only"],
    )
    tools = await client.connect()
    print("read-only tools:", tools)
    hits = await client.search("search_gmail_messages", "from:someone@example.com", top_n=5)
    print(hits)
    await client.aclose()


if __name__ == "__main__":
    asyncio.run(_demo())
```

Key SDK shapes (official quickstart): `StdioServerParameters(command, args, env)` →
`stdio_client(params)` yields `(read, write)` → `ClientSession(read, write)` →
`await session.initialize()`. `await session.list_tools()` returns an object with `.tools`
(each `tool.name`, `tool.description`, `tool.inputSchema`). `await session.call_tool(name, args)`
returns a result whose `.content` is a list of content blocks (`block.text` for text).

We will run this loop on a background thread/event loop inside the Flask process (the embedded
client), surface only the wrapped read search to `CappedToolLayer`, and never register or call a
write tool.

---

## Blockers the user must act on

1. **Gmail requires the user to create their own Google Cloud OAuth client (unavoidable).**
   `google_workspace_mcp` has **no shared/default OAuth client**. The user must create a Google
   Cloud project, enable the Gmail API, create an OAuth client ID, and set
   `GOOGLE_OAUTH_CLIENT_ID` (+ secret, or a public PKCE client). For personal use this is a
   one-time ~10-minute setup; it is **not avoidable** with this (or any of the surveyed Gmail)
   server — all surveyed Gmail servers require user-supplied GCP credentials. The consent screen
   may sit in "testing" mode (fine for the user's own account, just adds an "unverified app"
   warning at consent).

2. **Outlook needs NO Azure app registration for a personal account (built-in app + device
   code).** This is the easy path. Caveat: if the user is on a **work/school tenant whose admin
   restricts third-party apps / requires admin consent**, the built-in app may be blocked and a
   custom Azure app registration (`MS365_MCP_CLIENT_ID`/`TENANT_ID`) would then be required. For
   a personal Outlook.com account, no action needed beyond the device-code login.

3. **Two runtimes to bundle/require:** Python (Gmail, already present) **and Node.js ≥20**
   (Outlook). Document the Node dependency in setup. If shipping Node is undesirable, the Gmail
   path alone is fully Python-native.

4. **Write-tool exposure is the main risk to guard, and it's solvable.** Both recommended
   servers default to exposing write/send tools; we MUST pass the read-only flags
   (`--read-only`, plus `--tools gmail`/`--preset mail`) AND assert in the client (see sketch)
   that no send/delete/modify/draft tool is registered before issuing any call. **Read-only-only
   toolsets are achievable** on both recommended servers.

---

## Bottom line

- **Gmail:** `taylorwilsdon/google_workspace_mcp` — strongest because of the first-class
  `--read-only` flag (read scopes + write tools removed), Python-native, active, MIT, stdio.
- **Outlook:** `Softeria/ms-365-mcp-server` — strongest because personal accounts need **no
  Azure app registration** (built-in app + device-code), plus `--read-only` and a mail preset.
- **Biggest user action:** create a **Google Cloud OAuth client** for Gmail (unavoidable);
  Outlook needs nothing for personal accounts.
- **Read-only-only toolsets:** **achievable** on both via flags + a client-side assertion.

## Sources
- Gmail (recommended): https://github.com/taylorwilsdon/google_workspace_mcp
- Gmail (archived/full): https://github.com/GongRzhe/Gmail-MCP-Server
- Gmail (read-only, immature): https://github.com/Maheidem/gmail-mcp
- Outlook (recommended): https://github.com/softeria/ms-365-mcp-server
- Outlook (alternatives): https://github.com/ryaker/outlook-mcp , https://github.com/nsakki55/outlook-mcp , https://github.com/mcp-z/mcp-outlook , https://github.com/mpalermiti/outlook-mcp
- MCP Python SDK: https://github.com/modelcontextprotocol/python-sdk
- MCP client tutorial (Python): https://modelcontextprotocol.io/docs/develop/build-client
