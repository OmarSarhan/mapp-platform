# Connecting an agent to MAPP

For an operator doing this for the first time. It assumes you can already open
the dashboard and start the platform; it assumes nothing about MCP, OAuth or
what an agent is allowed to do here.

By the end you will have an AI assistant that can read your instance, propose
changes to it, and — if you choose — apply them with your permission. About
twenty minutes, most of which is reading.

Every number in this guide is checked against the code by
`scripts/tests/test_documentation_claims.py`. If one is wrong, that test fails.

---

## What this actually is

MAPP publishes an **MCP server**: a fixed set of tools an AI assistant can
call. It is not the configuration API with a chat interface in front of it —
it is a deliberately narrower surface, 56 tools against the platform's 73
actions, and the things left out were left out on purpose.

Three facts decide everything else:

1. **An agent sees only the tools its permissions allow.** Not "sees them and
   is refused" — they are absent from the list it is given.
2. **Nothing consequential happens without you.** 42 of the 56 tools only
   read. Of the 14 that write, 7 write into a review queue you work through,
   and the remaining 7 ask your permission at the moment they run.
3. **Permission is for one exact request**, not for a period or a kind of
   action. Saying yes to applying one proposal authorises that proposal and
   nothing else.

---

## Before you start

You need the platform running and the administrator password. If you have lost
the password:

```bash
./bin/mapp reset-config-password
```

---

## Step 1 — turn the agent surface on

It is off by default. A deployment that never issues an agent credential does
not run an OAuth server it has no use for.

```bash
MAPP_MCP=1 ./bin/mapp all
```

Or set it once for the deployment, in `.env`:

```
MAPP_MCP=1
```

An `.env` created before this key existed will not have it. `./bin/mapp
doctor` reports it as missing and `./bin/mapp doctor --add-missing` adds it,
commented default and all, without touching anything else.

> The shell takes precedence over `.env`, so `MAPP_MCP=1 ./bin/mapp all` turns
> the surface on for one command whatever `.env` says. Whichever you use, it
> has to be visible to every command that should see these services — `verify`,
> `ps` and `down` included — which is the reason to prefer `.env`.

Two services appear: `mcp-auth` (the authorization server) and `mapp-mcp` (the
tools). Check they are healthy:

```bash
./bin/mapp ps
```

The agent surface lives on its own origin, `http://localhost:8181` by default —
separate from the map and the dashboard. The port is what separates it; a
different port is a different origin as far as a browser is concerned.

> **Why a port rather than a name like `mcp.localhost`.** MCP clients refuse to
> send credentials to an `http://` token endpoint unless the host is literally
> `localhost`, `127.0.0.1` or `::1`. A subdomain is not exempt, so a `.localhost`
> name gets you through sign-in and consent and then fails at the token request,
> with the only evidence in the client's own log. authlib applies the same rule
> on the server side. Over HTTPS in production none of this applies, and
> `PRODUCTION_MCP_SITE` is a normal hostname.

---

## Step 2 — register the assistant

An agent is a **public client**: it proves itself with PKCE and holds no
secret, so there is nothing to copy down and nothing to keep safe.

The easiest route is the dashboard — **Access & audit → Agent access → MCP
agent clients**. Register a client, then choose **Claude** or **Codex** above
the configuration to copy the appropriate format and command. Use a separate
registration for each assistant when you want independent consent, revocation,
and standing-approval switches. From a terminal:

```bash
./bin/mapp mcp-client-register --name "Claude Code" \
  --redirect-uri http://localhost:8484/callback \
  --redirect-uri http://127.0.0.1:8484/callback \
  --scope mcp:connect --scope inspect --scope derive --scope semantic:inspect
```

It prints a client ID like `mcp-NgbtAji4QKqEGrLa`.

> **Register both loopback spellings.** Redirect URIs are matched byte for
> byte, with no flexibility about `localhost` versus `127.0.0.1`. Claude Code
> has switched between the two across versions, and a client that sends the
> spelling you did not register cannot sign in.

---

## Step 3 — point the assistant at it

Use the client ID and MCP URL from your dashboard in the examples below.

### Claude Code

```bash
claude mcp add --transport http \
  --client-id mcp-NgbtAji4QKqEGrLa --callback-port 8484 \
  mapp http://localhost:8181/mcp
```

Or as a project `.mcp.json`:

```json
{
  "mcpServers": {
    "mapp": {
      "type": "http",
      "url": "http://localhost:8181/mcp",
      "oauth": {
        "clientId": "mcp-NgbtAji4QKqEGrLa",
        "callbackPort": 8484,
        "scopes": "mcp:connect inspect derive semantic:inspect"
      }
    }
  }
}
```

> **The `oauth` block is not optional.** Without it the client tries Dynamic
> Client Registration, which this server refuses deliberately — a person
> decides which agents may ask for permission, not the agents.

> **Fix the callback port.** Without `--callback-port` the client picks an
> ephemeral one, and a port nobody can predict cannot be registered in advance.
> Exact matching and dynamic ports are mutually exclusive. `8484` is what the
> dashboard uses.

Open `/mcp` in Claude Code, select `mapp`, and authenticate. Approve the project
server if Claude asks. See [Claude Code's MCP setup guide](https://code.claude.com/docs/en/mcp)
for configuration scopes and the browser sign-in flow.

### Codex CLI

Choose **Codex** in the dashboard and merge its TOML into
`~/.codex/config.toml`, or `.codex/config.toml` in a trusted project. Keep any
existing settings and add only one `mapp` entry. The configuration uses the
[documented Codex OAuth fields](https://developers.openai.com/codex/config-reference):

```toml
[mcp_servers.mapp]
url = "http://localhost:8181/mcp"
scopes = ["mcp:connect", "inspect", "derive", "semantic:inspect"]

[mcp_servers.mapp.oauth]
client_id = "mcp-NgbtAji4QKqEGrLa"
callback_url = "http://127.0.0.1:8484/callback"
callback_port = 8484
```

Set both the callback URL and listener port to match the registered URI. MAPP
advertises issuer identification, which lets Codex reuse this fixed callback.
Use a current Codex release supporting these OAuth settings; a generated
callback suffix or random port will not match MAPP's registration.
[Codex callback rules](https://developers.openai.com/codex/mcp).

After saving, run this from the configured project:

```sh
codex mcp login mapp --scopes mcp:connect,inspect,derive,semantic:inspect
codex mcp list
```

The dashboard generates the scope list for your chosen permissions. Pass that
list explicitly: MAPP's discovery metadata advertises only bootstrap scopes,
which are insufficient for analysis tools. Complete browser consent, restart
your Codex session, then use `/mcp` to inspect the connection.
[Codex login command](https://developers.openai.com/codex/cli/reference).

### Codex extension for VS Code

Install the OpenAI Codex extension and sign in to Codex. Use the same **Codex**
TOML above: the CLI and extension share configuration on the same host. This
configuration belongs in `.codex/config.toml` or `~/.codex/config.toml`, not
VS Code's `.vscode/mcp.json`.

Run the generated `codex mcp login mapp --scopes ...` command in the same
environment as the extension, so it uses the same configuration and credential
store. In the Codex gear menu, open **MCP servers**, then **Restart extension**
after saving. Confirm `mapp` is enabled and connected.
[Codex IDE MCP setup](https://developers.openai.com/codex/mcp).

For WSL, SSH, or a dev container, `localhost` means the environment running
Codex. Use a reachable MCP URL and forward callback port `8484` from the browser
machine to that environment. Complete one assistant's login at a time so both
clients do not compete for the callback port.

---

## Step 4 — the first connection

Your assistant opens a browser at `localhost:8181`. You will:

1. **Sign in** with the administrator password. This is the authorization
   server, not the dashboard, so it asks even if the dashboard is already open
   in another tab.
2. **See a consent screen** naming the assistant and every permission it is
   asking for, in plain words.
3. **Approve**, and the browser hands a credential back to the assistant.

That credential is short-lived and refreshes itself. You will not do this
again unless you revoke the consent or change the permissions.

Ask the assistant to list the layers in your workspace. If it can, you are
connected.

---

## Choosing permissions

Permissions are exact and non-hierarchical — there is no "admin" that implies
the rest, and the widest scope the platform has (`full`) is never issued to an
agent at all. Six presets, and the tool counts are what an agent holding that
preset is actually shown:

| Preset | Tools | What it can do |
| --- | --- | --- |
| **Discovery** | 21 | Find out what exists. The platform contract, the layer list, capabilities |
| **Analysis** | 34 | Read the workspace properly: layer configuration, catalogue, aggregate values, curated meaning. **The sensible default** |
| **Analysis (federated)** | 37 | The same, plus which third-party databases this instance reads |
| **Author** | 44 | Compose changes and attach rendered evidence to them. Applies nothing |
| **Author and apply** | 47 | The above, plus applying a reviewed proposal and reloading the map |
| **Author, apply and build derived layers** | 52 | The above, plus creating and dropping the database relations layers read. The widest offered |

Start with **Analysis**. Widening later is one dashboard change and one
reconnection.

Why even the widest preset does not reach all 56: two scopes sit outside it.
`federation:observe` reveals which third-party databases this instance reads —
offered, but only through *Analysis (federated)*, so you choose it rather than
inherit it. `semantic:source` lists database tables no layer uses, and is in no
preset at all. Both are disclosure rather than authority, which is why neither
is bundled with anything else.

---

## What "it asks you" means

This is the part worth understanding before granting `apply`.

When an agent calls a tool that changes something, the platform does **not**
simply let it. It records exactly what is about to happen — the operation, the
arguments, the revision — and refuses to proceed until a person agrees to that
specific thing.

**You are asked in the session you are working in.** Your assistant shows a
prompt: what it wants to do, in one line, composed from the platform's own
record of the change rather than by the assistant. Answer yes and it proceeds;
answer no and nothing happens.

Three properties are worth knowing, because they are what make this meaningful
rather than a dialog box:

- **Permission is bound to one exact request.** Not to the operation, not to
  the tool. If anything about the request differs from what you were shown —
  a different argument, a moved revision — the permission does not fit it.
- **It is used once.** Spent when the change happens, and never again.
- **It expires.** Fifteen minutes to decide; five minutes from your decision
  to act on it. An approval left lying around is not a standing permission.

If your assistant cannot show prompts, it will instead give you a link to the
dashboard and ask you to try again after you approve there. Same permission,
one extra step.

### What asks, and what does not

**Asks you** (7): applying a workspace proposal, applying a semantic proposal,
reloading the map, and creating, replacing, refreshing or dropping a derived
layer.

**Does not ask** (7): creating a workspace or semantic proposal, the preview
tools that attach evidence, and rejecting a workspace proposal. Rejection can
trigger cleanup of disposable draft relations under the policy already approved
when they were created. Permanent relations are unaffected.

**Reads** (42): everything else.

### Dropping a derived layer

The one genuinely destructive tool, and it is guarded four ways: the relation
must be named exactly; the drop is refused outright while any layer or other
derived relation still reads it; the prompt names what would break; and the
permission is single-use and bound to that one relation.

---

## Standing approvals

In **Security → Standing approvals**, turn on automatic approval for an MCP
client. It can then perform any action its current permissions allow,
including semantic administration and federation changes, until you turn it
off. There is no time or action limit and no action-class selector.

The approval is bound to that registered client and this platform instance.
It applies across the client's consents, while every request must still have
a live consent and sufficient scopes. Revoking a consent stops its credentials;
a different valid consent for the same client can still use the standing
approval. Disabling the client turns its standing approval off.

If you signed in more than 15 minutes ago, sign in again to enable it. Only an
administrator browser session can change the switch; an agent cannot enable
its own approval. Turning it off also invalidates unused automatic approval
receipts. A recovery-epoch advance turns it off after a restore.

Every action still uses a single-use, request-bound receipt, passes the usual
revision, validation and resource checks, and is audited against the client
approval that allowed it. Existing bounded approval windows are closed during
the upgrade; enable the new switch deliberately for each client you trust.

---

## Taking it away

- **One agent:** Security → MCP clients → withdraw. Immediate, including
  credentials already issued.
- **One consent:** Security → MCP grants → revoke. Also closes any standing
  approvals it holds.
- **Everything:** stop the platform without `MAPP_MCP=1`. The services do not
  start and the surface is gone.

---

## When it does not work

**"The client does not exist on this server."** The client ID is wrong, or it
was withdrawn. `./bin/mapp mcp-client-list` shows every one and its state.

**Sign-in bounces back to the consent screen.** Usually the redirect URI.
Register both `localhost` and `127.0.0.1` spellings, and check the callback
port matches what you registered.

**The assistant sees no tools, or fewer than expected.** That is the permission
filter working. Check the client's scopes against the preset table above —
tools an agent cannot call are not shown to it.

**The assistant still shows the pre-upgrade tool arguments or is missing new
tools after the MCP service was rebuilt.** MCP clients commonly cache the
`tools/list` result for the life of their session. Reconnect or restart that
client session, then call `describe_instance`; the current expanded visual
surface reports `mapp-mcp/0.5.0`. Re-authorize only when the reconnected client
is missing the required scope. A second authenticated session seeing newer
tools is evidence that the first session's manifest is stale, not that the
configuration API lacks the operation.

**Everything refuses with a validation error naming a table.** Not an MCP
problem. The workspace fails validation, which blocks proposing and applying
alike. If the table is in a federated schema, check the source is still
verified — the platform withdraws access to a source it cannot verify, and
that surfaces as exactly this error.

**After a redeploy, federated sources stop working.** Start the platform
through `./bin/mapp`, not a hand-written `docker compose`. Settings in `.env`
select extra compose overlays, and a command that omits them silently drops the
credentials those sources need.

---

## What the agent is told

Alongside the tools, the server publishes three **MCP resources** — documents
an assistant can read without calling anything:

| Resource | Covers |
| --- | --- |
| `mapp://guidance/workflow` | The order the platform expects, and the safeguards that are not negotiable |
| `mapp://guidance/styling` | Which property actually carries a colour, and how to choose graduated breaks |
| `mapp://guidance/derived-layers` | Views against materialized relations, naming, spatial scope, and what guards a drop |

These exist because most of what makes a change *good* here cannot be inferred
from a tool schema. That a `circle` point takes its colour from
`strokeColor` and a `dot` from `fillColor`, that a value displaying as `0.0%`
is not zero, that a replace quietly changes the numbers every dependent layer
returns — an assistant that does not know these produces changes that apply
cleanly and are wrong.

They are adapted from the CLI's agent workflow rather than copied from it: the
judgement carries across, the invocations do not. Nothing in them is
instance-specific, so every connected assistant can read them whatever its
scopes.

---

## Where to read further

- [`mcp-authorization.md`](mcp-authorization.md) — how the credentials work,
  what each component does, and every operational decision behind them.
- [`mcp-threat-model.md`](mcp-threat-model.md) — what this defends against,
  what it does not, and the residual risks stated plainly.

### Original-resolution preview downloads

`artifacts_image(artifact_path=..., download="link")` returns a five-minute
signed PNG download without embedding the image in chat. Configure
`ARTIFACT_DOWNLOAD_ORIGIN` when the user's browser reaches MCP through a
different public origin or tunnel. It defaults to `MCP_SITE` in development
and `PRODUCTION_MCP_SITE` in production. Forward `/artifact-downloads/*`
through the same MCP-facing Caddy host. Remote origins must use HTTPS.

No dashboard login or platform bearer token is needed to download. Anyone
holding the link can read that one image until expiry; treat links as private.
Expired links can be reissued through the image tool. Service restarts invalidate
outstanding links. Chat display size is independent of the original PNG size.
