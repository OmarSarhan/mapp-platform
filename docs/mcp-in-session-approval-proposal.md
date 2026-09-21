# Proposal: approve in the MCP session

**Status:** drafted, not applied. It weakens a documented security posture, so
it wants a deliberate decision rather than a quiet commit.

**Asked for by the owner on 2026-09-20**, restating Phase 1 decision 4 after
that decision was reported as unreachable: *"I want it so the person/agent
driving the MCP session approves, in that session, not an operator at a
separate dashboard."*

## It works. This was measured, not designed on paper.

With the two edits below applied to a scratch build and driven by a real OAuth
client against the deployed stack:

```
server asked: {"mode": "form", "message": "MAPP wants to run xyz.reload.
               Reload the map from the workspace on disk.", ...}
client answered: accept -> 202
elicitations seen: 1
result: {"requestedGeneration": 67, ..., "healthy": true, "completed": true}
```

One tool call. The person answered a prompt their own client rendered, the
receipt was minted and spent, and the tile service reloaded. No dashboard, no
second call.

For contrast, the same call on the current build:

```
This needs your approval before it can happen. Reload the map from the
workspace on disk. Approve it here: http://config.localhost/#approvals/bae4...
-- then ask me to try again and I will pick up your decision.
```

## The two edits

**1. `mapp-mcp/runtime.py`, in `build_runtime_app`** — `stateless_http=True`
becomes `stateless_http=False`.

**2. `mapp-mcp/era_guard.py`, `_strip_session`** — stop removing
`Mcp-Session-Id` from responses. The guard refuses to forward a session the
server has been told to forget, so with sessions on, stripping the header makes
every request after `initialize` fail.

That is the whole functional change. Everything else below is consequence.

## What it costs, stated exactly

`era_guard` obligation 4 is *"Never mint or echo `Mcp-Session-Id`."* This
trades it away. Being precise about what that obligation was worth:

- **It never enforced the era decision.** `era_guard` does that, on the wire,
  by holding the served set to `SERVED_VERSIONS`, and it still does. The
  runtime's own comment already said so: *"This is a consequence of the era
  decision, not the control that enforces it."*
- **What it did buy** is a smaller surface: no server-side state keyed by an
  identifier a client presents. That is what is given up.

## What does not change, checked rather than assumed

- **A session identifier authorises nothing.** Authentication is per request
  from the bearer token; `CURRENT_CALLER` is set from it on every call. A
  stolen session id with no token A reaches nothing.
- **One session cannot answer another's elicitation.** This is the property the
  whole approval mechanism rests on, so it was probed rather than reasoned
  about. Two sessions were opened on one valid token. The server asked the
  first; the second answered with the correct request id. The transport acked
  it `202` and never routed it — the first call stayed waiting and never
  proceeded. An agent cannot approve a mutation it was not asked about.

  This needs a test before the change lands. `CrossSessionElicitationTests`,
  driving two sessions through the composed application, asserting the victim's
  call does not complete on the attacker's answer.

## Tests that must change in the same commit

| Test | Why |
| --- | --- |
| `test_era_guard.SessionHeaderTests.test_a_session_id_from_the_runtime_is_stripped` | Asserts the strip that is being removed |
| `test_era_guard.SessionHeaderTests.test_it_is_stripped_on_pass_through_paths_too` | Same |
| `test_legacy_session.test_no_session_identifier_is_ever_minted` | Asserts the obligation being traded |

Each should become its inverse with the reasoning attached, not be deleted —
the day somebody wonders why this server keeps sessions, the test is where the
answer should be.

## Documents that must change

- **`era_guard`'s module docstring, obligation 4.** It is the statement of
  record and it becomes false.
- **`docs/mcp-threat-model.md`**, the elicitation section. It currently says
  the dashboard is the working path and elicitation cannot run, with the
  measurement. That measurement stays true of the *old* configuration and
  should be recorded as the reason this changed, not deleted.
- **`docs/mcp-phase1-plan.md`**, wave 5. Decision 4 goes from overturned back
  to honoured, by this change, on this date.

## What stays true either way

The dashboard path is not removed by this. A client that declares no
elicitation capability — Gemini CLI 0.58.0, measured 2026-09-18 — still gets
the two-call dashboard flow, and that code and its tests stay. What changes is
that clients which *can* be asked now are.

## The recommendation

Apply it. The owner asked for it twice, the property that matters was measured
rather than assumed, and the obligation being traded was never the control it
looked like. But it should land as its own commit, with the cross-session test
written first, so that the trade is legible in the history rather than folded
into a feature.
