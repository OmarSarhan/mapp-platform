# mapp-mcp Phase 1 — mutation, in waves

Phase 0 shipped an authorization broker, a request-bound token exchange and a
37-tool read surface, all merged and opt-in behind a Compose profile. Phase 1
gives an agent the other half of the platform's own loop: propose, review,
apply.

This plans the order. Two of the waves change what an existing scope means,
one lifts a pin that was set deliberately, and one adds a record type the
specification has been reserving envelope members for since P10.

The five decisions the plan turned on were settled on 2026-09-18 and are
recorded in *Decisions taken* below. The largest — that the person driving the
MCP session approves, in that session — was answered before wave 5 was
designed, and the elicitation capabilities of all three shipped clients were
measured rather than assumed before the mechanism was chosen.

## What already exists

Not a small amount, which is why the early waves are cheaper than they look.

- **Token B is already single-use when mutating.** The flag is derived from the
  platform's risk class, and eleven drift guards assert the three declaration
  sites agree. Nothing about the credential needs redesigning to mutate.
- **The execution envelope already has the approval members.** `mapp-jcs-v1`
  carries `resolvedDefaults`, `confirmationFields` and `revisionBinding` as
  explicit nulls, required at every call site precisely so that wiring them is
  a visible edit rather than a silent default.
- **Four mutating operations are already allowlisted** — `proposals.apply`,
  `semantic.proposals.apply`, `federation.aliases.observe` and
  `derived-layers.refresh` — with no tools behind them.
- **The loop does not need the whole workspace.** `proposals.check` and
  `proposals.create` both take `operations` and `revision`, not a candidate
  document. `workspace get`, which was ruled out of the read surface, is not a
  prerequisite for proposing. `validate` remains unbuilt and unnecessary:
  `proposals.check` is the agent-shaped equivalent.

## The surface being planned against

33 mutating actions across ten scopes. The workspace loop is four of them:

| Step | Action | Scope | Notes |
| --- | --- | --- | --- |
| Check | `proposals.check` | `propose` | Read risk. Validates operations, returns a `checkFingerprint` |
| Propose | `proposals.create` | `propose` | Takes the fingerprint, writes a proposal record |
| Review | `proposals.preview-*`, `proposals.screenshot` | `visual` | Evidence for a human |
| Apply | `proposals.apply` | `apply` | Then `xyz.reload` under `reload` |

Semantic work mirrors it under `semantic:propose` and `semantic:apply`.
Derived layers do not use proposals at all: `create`, `replace`, `refresh` and
`drop` act directly, which is why they need their own treatment.

---

## Wave 1 — split `derive`, before anything mutates

**Nothing else can safely start.** `derive` today authorises both reading
aggregate values from a managed relation and creating, replacing or dropping
one. It covers seven mutating actions, and it is in the dashboard's default
`analysis` preset — so the moment any of those gets a tool, an ordinary
read-only analysis grant can drop a derived layer.

This is not hypothetical drift. `derived-layers.refresh` is allowlisted today
and needs only `derive`; it is held shut by having no tool, which is one
accident away from being no protection at all.

**Build:** a second scope for the managed-relation lifecycle — working name
`derive:manage` — reassigned across `derived-layers.create`, `.replace`,
`.drop`, `.plan`, `.plan-area-weighted-h3`, `.refresh` and
`operations.cancel`. `derive` keeps its read meaning: `layers_values`,
`layers_statistics`, `sql_test`, `operations_show`. Platform classifier,
action table, broker allowlist, dashboard options.

**Decided:** `operations.cancel` goes with the lifecycle. Cancelling a job is
a write — it stops work someone else may be waiting on — so it belongs with
the scope that authorises starting one, not with the reads.

**Verify:** the existing threat-model assertion that only
`derived-layers.refresh` is reachable by an offered scope should become "no
mutating action is reachable by an offered scope", and that is the test that
says this wave worked.

No new tools. The surface does not change; what a grant means does.

## Wave 2 — check, the read that costs a write scope

**Build:** `proposals_check` and `semantic_proposals_check`. Both are read-risk
actions that cost a propose scope: they validate operations against a revision
and return warnings and a `checkFingerprint` without writing anything.

**Why first among the loop:** it is the only mutation-adjacent action that
cannot change the workspace, so it exercises the whole path — a propose-class
scope, a POST body under digest, the refusal vocabulary — with nothing at
stake. It is also what makes the next wave honest, because `proposals.create`
takes the fingerprint this produces.

**Watch for:** the same body-digest agreement `sql_test` needed. This will be
the second and third POST tools, and the first where the body is large enough
to make canonicalization disagreements plausible.

## Wave 3 — propose

**Build:** `proposals_create` and `semantic_proposals_create`, both requiring
the fingerprint from wave 2.

**Why it is safe without approval machinery:** proposing writes a proposal
record and touches no workspace. The platform already treats the queue as the
safe half of the loop, and `proposals_show` has been readable since wave 5 of
Phase 0, so a person can review what an agent proposed with the tools that
already exist.

**Decided:** `propose` gets its own preset and is never folded into
`analysis`. A grant that can read an instance and a grant that can add to its
review queue are different things to hand someone, and a preset is what an
operator actually clicks.

**This is the first wave where an agent changes durable state.** It should end
with a real client driving the full sequence — read the workspace, compose
operations, check, propose — and a person reading the result in the dashboard.

## Wave 4 — review evidence

**Build:** `proposals_preview_plan`, `proposals_preview_test`,
`proposals_preview_screenshot` under `visual`.

**Why here:** a proposal a human must review is worth more with evidence
attached, and these produce it without applying anything. They are also the
first tools that create artifacts and run asynchronously, which is what makes
`operations_show` — already built — load-bearing rather than decorative.

**Decided:** `visual` comes off `NEVER_OFFERED_TO_AGENTS`. It is necessary —
without it an agent can propose but cannot attach the evidence a person needs
to decide, which makes the review step worse than not having the agent. The
pin is lifted the way `semantic:source` was: reclassified deliberately, with
the reasoning recorded where the pin used to be, and kept out of the read-only
presets so granting it stays a choice.

**Watch for:** a screenshot renders the workspace through a real browser, so
this is the first scope that spends meaningful compute on an agent's say-so.
The browser-runner already bounds that; the wave should confirm it rather than
assume it.

## Wave 5 — approval intent and receipt

The substantial one, and the reason the four waves above come first: by here,
everything an agent can do is either reversible or reviewable by a person.

**Build:** the `control` records the specification has been reserving —
approval intent carrying the canonical execution digest and its bindings, and
a single-use receipt bound to that one digest, consumed atomically with the
effect. Plus the approval page the URL path opens, the dashboard surface for
clients that can elicit neither, and audit events for both decisions.

**This is where the envelope's three null members stop being null.**
`resolvedDefaults`, `confirmationFields` and `revisionBinding` were made
required arguments so that wiring them would be a visible edit at every call
site; this is that edit.

**Decided: the person driving the MCP session approves, in their own session,
if their grant carries the right and they are satisfied with the evidence.**
Not an operator at a separate dashboard. Routing an agent's mutation through a
different surface makes the loop unusable in a chat, which is the context this
whole component exists for.

The mechanism is MCP elicitation, and the capability is declared at
`initialize`, so the server knows per session which path it can take. Measured
against the three shipped clients on 2026-09-18:

| Client | Advertises | Approval path available |
| --- | --- | --- |
| Codex CLI 0.155.0 | `elicitation: {form, url}` | URL — send the person to the approval page |
| Claude Code 2.1.276 | `elicitation: {}` | Form — the client prompts; evidence must be in the tool reply |
| Gemini CLI 0.58.0 | none | Dashboard fallback |

So three paths, chosen by what the client can do rather than by preference:

1. **URL elicitation** where offered. The tool call pauses, the person opens an
   authenticated approval page showing the diff and the rendered evidence, and
   decides there. This is the best version: the decision is made in a browser
   session the agent does not control, looking at the actual visual.
2. **Form elicitation** otherwise. The tool returns the summary and a link to
   the evidence, and the client prompts the person to confirm. The prompt is
   rendered by the client, not composed by the model.
3. **Dashboard** for clients advertising neither, which today is Gemini.

**The property that has to hold in all three:** the agent must not be able to
approve its own request. Form and URL elicitation both satisfy this because the
*client* renders the prompt and the *person* answers it — the model cannot
fabricate an `ElicitResult` any more than it can fabricate a tool result it
did not receive. A confirmation value merely returned to the agent and echoed
back would not satisfy it, and is the obvious wrong design here.

**Therefore the receipt is bound to the elicitation outcome, not to a value the
agent holds.** The intent carries the canonical execution digest; the approval
resolves against that intent; the receipt is minted by the platform and spent
by the platform. Nothing approval-shaped is ever a tool argument.

**Watch for:** a person approving in their own session is the same person who
asked for the change, so this is a confirmation control rather than a
segregation-of-duties control. It defends against an agent doing something the
person did not intend; it does not defend against a person doing something they
should not. That is the right trade for this system and should be stated in the
threat model rather than left to be inferred.

**Built.** What landed, and the two places it differs from the plan above:

- The requirement is **derived from the action's own risk class**, not declared
  a fourth time. `control_api.requires_approval` states the *exemption* set, so
  a risk class nobody has classified requires approval rather than arriving
  unguarded. Four allowlisted operations require one today: `proposals.apply`,
  `semantic.proposals.apply`, `derived-layers.refresh` and
  `federation.aliases.observe`.
- Enforcement is at `_redeem_exchanged_token`, the one boundary every exchanged
  credential already passes, against the digest computed there. A missing
  receipt is refused *before* the credential is spent, so a forgotten header
  does not also cost an exchange.
- Form mode needed a fourth approvals operation the plan did not name.
  `approvals.confirm` relays a decision a person made in their MCP client,
  because the dashboard decide route requires an operator session an MCP caller
  does not have. It records `session:<grant>` as the decider so the audit can
  tell the two assurances apart. The platform cannot verify the elicitation;
  that division of trust is in the threat model.
- The receipt is bounded twice, not once. `RECEIPT_LIFETIME` is derived from
  `decided_at` rather than stored, so somebody who takes fourteen minutes to
  decide does not leave the agent one minute to act — and an approved receipt
  is not a standing authorisation.

**Corrected 2026-09-20, after a real client was driven through the whole
loop.** Four defects had shipped, every suite green, and all four were the same
kind: nothing executed the route.

- **The three approvals routes were not in `_required_scope`**, so they fell
  through to the `full` catch-all the broker never issues. The entire gate was
  unreachable — every intent refused `auth.scope_required`. The guard that
  should have caught this, `RouteGateAlignmentTests`, checked a hand-written
  table of five operations; it now derives one concrete path per allowlisted
  operation, so a new one is covered the day it is added.
- **`approvals.create` demanded a bare 64-character sha256**, while every
  digest in the system is scheme-prefixed and the receipt is matched against
  the prefixed form. No approval could ever have matched. The store tests used
  a bare fixture and agreed with it.
- **The handler referred to an undefined `CONFIG_SITE`**, and recorded the
  dashboard origin as the approval's `instance`.
- **A retry created a second pending approval**, so the person approved the
  first and the agent waited on the second. Even the dashboard path could not
  complete. The runtime now remembers the handle for one grant and one digest,
  which is also why the handle never has to travel through the model.

**Elicitation could not run at all, and now can.** Measured, not inferred:
under `stateless_http` neither served era carried a server-initiated request.
That made the dashboard the working path rather than the fallback, so it became
a deliberate two-call flow — ask and say where, then pick up the answer — and
that flow remains for clients which declare no elicitation capability.

At **wave 7** the owner restated decision 4 after being shown it was
unreachable, and sessions were enabled to honour it: `stateless_http=False`,
and `era_guard` no longer strips `Mcp-Session-Id`. Obligation 4 was withdrawn
with it, deliberately and in the guard's own docstring. It never enforced the
era decision — obligations 1 to 3 do that and are untouched — so what was given
up is a smaller surface, not a control on which revisions are served. A person
now approves in one tool call, in the session they are working in, which is
what decision 4 asked for.

**Proved end to end on 2026-09-20** against the deployed stack with a real
OAuth client: authorize with PKCE → consent → token A → `tools/list` → ask →
an operator approves in the dashboard → the receipt is claimed and spent → the
tile service reloads, generation 66 applied. A third call is refused, because
the approval was single-use.

**Proved on 2026-09-21, including the write.** A real OAuth client ran
`proposals_check` → `proposals_create` → `proposals_apply`, approved the apply
*in the session* on the one elicitation, and the workspace changed: `applied:
true`, `mapReloaded: true`, a new revision, and the layer's name was what the
proposal said. The loop was then run in reverse to restore it, which proved it
twice.

**A correction to the earlier account.** The validation failure that blocked
this — `locale.layers.Bus_Stops.tables.15: Table is not selectable through the
configured read-only connection` — was reported here as pre-existing. It was
not: it was caused by redeploying `config-ui` with a hand-written `docker
compose` invocation that omitted `compose.federated-demo.yaml`, which is the
overlay carrying the `FEDERATION_DBS_*` credentials. Without them federation
verification cannot reach a source, and the platform then *withdraws consumer
access on purpose* — `mark_unverifiable`, "consumer access withdrawn until a
pass completes". So the missing grant was the platform enforcing a policy, not
a permission somebody forgot, and granting it by hand was undone by the next
verification pass. Deploying through the overlays `./bin/mapp` composes
restored both aliases to `active` and the grants with them, with nothing
granted by hand.

The lesson is the deployment command, not the platform: `MAPP_DEMO_SOURCES` in
`.env` selects two further overlays, and `bin/mapp` assembles them. A bare
`docker compose --file compose.yaml --file compose.bundled-db.yaml` is not the
deployed configuration, and the way it fails is a confusing error about a table
rather than anything naming credentials.

**Worth knowing:** a receipt is spent at the authorisation boundary, so a
request the platform then refuses on a business rule has still consumed its
approval. Safe, and the same semantics as the single-use credential, but it
means a person re-approves after a refusal.

**Superseded — the original note, kept for the reasoning:** Wave 5 builds the
gate; no tool calls it yet, because the tools that need it are wave 6. The gate
is exercised directly by 28 tests and the enforcement by the configuration API's
own suite, but "a real client drove a real approval to a real effect" is wave
6's first acceptance run, not this one. A guard is in place for that join:
`GatedToolTests` fails if a tool spends an operation that requires approval
without going through `approval_gate`, which is vacuous today and deliberate —
the plan calls wave 6 "mostly the join", and a join is what gets made in one
place and forgotten in the second.

## Wave 6 — apply

**Build:** `proposals_apply` and `semantic_proposals_apply` behind a receipt,
then `xyz_reload` under `reload`.

Both are already allowlisted with no tool, so this wave is mostly the join
between wave 5's receipt and the credential the exchange already mints
single-use. `operations_show` follows the asynchronous half, and `xyz_status`
already answers whether the tile service picked the change up — the two tools
that looked like completeness in Phase 0 turn out to close this loop.

**Built.** `proposals_apply`, `semantic_proposals_apply` and `xyz_reload`, all
through wave 5's gate. What the plan did not say, and what this wave actually
turned on:

- **Three pins came off `NEVER_OFFERED_TO_AGENTS`**: `apply`, `semantic:apply`
  and `reload`. That is the substance of the wave rather than a consequence of
  it. Until now "a person still decides" held because those scopes could not be
  granted at all; now it holds because every operation they buy is refused
  without a receipt. The threat model says so in those terms, and the claim
  tests were rewritten to check the new control rather than the old one.
- **The dashboard had to grow three options and a preset.** A scope the broker
  will issue and no operator can grant makes a correct tool unusable — the
  failure the drift guard was written for. That guard covered reads only,
  because until now only reads were offerable; it now covers every allowlisted
  operation except what is deliberately pinned, and `authoring-apply` is the
  preset an operator actually clicks.
- **`xyz.reload` was not allowlisted and had to be**, for a narrower reason
  than the plan implies. `proposals.apply` already reloads. The case reload
  exists for is an apply that commits and then answers 504 because the reload
  was not observed — which is why `_applied` reports the write and the reload
  as two facts and tells the agent not to apply again.
- **Evidence had to be threaded, not assumed.** Visual results live on
  operations, not on the proposal record, so `proposals_apply` takes an
  `evidence_operation_id` and reads the outcome from the platform. Reading an
  operation costs `derive`, which every preset but `discovery` carries and a
  hand-picked grant may not; a grant without it gets a packet saying evidence
  was asked for and could not be read, rather than a failed apply or a silent
  omission.

**The join wave 5 could not demonstrate is now made**, and `GatedToolTests` is
no longer vacuous: it checks three real tools and fails if any of them stops
calling the gate.

## Wave 7 — the derived-layer lifecycle

**Build:** `derived_layers_create`, `_replace`, `_refresh`, `_drop` under wave
1's new scope and wave 5's receipts.

**Why last among the mutations:** these act directly with no proposal, no
review queue and no diff to read beforehand. Everything above leaves a record
a person can inspect before it takes effect; these do not, so they should
arrive only once approval is real.

**Decided:** an agent may drop, with guards. `drop` is the first genuinely
destructive tool on the surface and the guards are the substance of the wave,
not a caveat on it:

- **Name the relation exactly.** No pattern, no "the one I just made", no
  implicit target from conversation state.
- **Refuse if anything depends on it.** `dependencies_list` already answers
  which configured layers read a relation, and a drop that would break one is
  refused by name rather than approved and regretted. An agent that wants it
  gone anyway has to remove the dependents first, which is a separate approval
  each.
- **Show what breaks in the approval.** The intent carries the dependent list,
  so the person approving sees the consequence and not just the verb.
- **A receipt per drop.** Single-use, bound to that one relation; the
  credential is already single-use for mutating operations, so this is the
  approval matching the credential rather than new machinery.

The asymmetry is deliberate: creating is cheap to undo and dropping is not, so
they sit behind the same scope but not behind the same amount of ceremony.

**Built.** `derived_layers_plan`, `_create`, `_replace`, `_refresh` and
`_drop`. Five rather than the four planned: `plan` is the dry run, standing in
the same relation to `create` that `proposals_check` stands in to
`proposals_create`, and it matters more here — a derived layer leaves no
proposal, so the plan is the only description of the change that exists before
the change does. It is single-use anyway, because probing runs real database
work and a replayable credential for it is a way to spend that work twice.

`derive:manage` came off `NEVER_OFFERED_TO_AGENTS`, the last pin to go.
Nothing that mutates is held by unofferability any more; every one of them is
held by the receipt.

**The drop guards, which are the wave:**

- **Named exactly**, and the name is percent-encoded into the path, never
  interpreted. No pattern, no "the one I just made".
- **Dependents read before anybody is asked.** The platform refuses an in-use
  drop on its own; the tool does not rely on that and the platform does not
  rely on the tool. What the tool adds is that nobody is prompted to approve
  something that cannot happen.
- **What would break travels in the approval**, so the person sees the
  consequence and not just the verb. On a clean drop the list is empty and is
  carried anyway, so the approval shows the question was asked.
- **A receipt per drop**, single-use and bound to that one relation.

**`replace` turned out to need the same care, which the plan did not say.** A
drop announces itself — the platform refuses it while anything reads the
relation. A replace does not: every layer reading it keeps working and starts
returning different numbers. So its packet carries the current definition
beside the proposed one and the dependent list, because "what reads this" is
the question a person should be asked there and the operation does not ask it.

Two guards fired during the build and were right both times: the allowlist
drift test caught that `plan`, `create` and `replace` also need
`semantic:inspect` — the handler resolves semantic sources before probing, so
a credential minted for `derive:manage` alone is refused — and the risk-class
test caught an attempt to mark the dry run non-mutating without reclassifying
`database-plan`.

## Wave 8 — standing approval windows (P8)

**Build:** time-boxed windows binding grant, client, instance and one action
class, capped at 60 minutes and a consumption count, decremented atomically,
revocable from the dashboard, invalidated by grant revocation and by a
recovery-epoch advance.

**Never covering:** the semantic administration and federation mutation
classes in full, per the permission-class table — deliberately broader than
naming scopes, because a window binds action classes.

**Why last:** a window substitutes the decider, never the receipt. It cannot
be built before the receipt exists, and it should not be built before there is
operational experience of how often per-action approval actually bites.

**Built.** The record, the four bounds a CHECK can hold, the three it cannot,
the dashboard surface, and the substitution in `create_approval`.

**The unit is the risk class, derived not invented.** `ACTION_SCHEMAS[op]["risk"]`
is already how the approval requirement itself is derived, so a window binds
the same thing. `WINDOWABLE_ACTION_CLASSES` is an *allowlist* — the opposite
direction from `NO_APPROVAL_RISKS` — because here the safe default is that no
window may decide, so a class nobody has considered is un-windowable rather
than silently covered.

**O8 is answered without a new column.** "What timestamp establishes recent
authentication" is `control.sessions.created_at`, which is written when the
password verifies and never refreshed, so it is an authentication time rather
than an activity time. It has a visible consequence, stated rather than
designed around: an administrator whose session is older than fifteen minutes
must sign in again to open a window. That is the right friction for arming
auto-approval.

**The substitution is one place and one bit.** `create_approval` spends a
window *before* the insert — a window found and not spent would be a race that
approved more than it authorised — and the row is inserted `approved` with the
window creator as its decider. The route reports `decided`, and `approval_gate`
claims the receipt and returns instead of asking. The runtime does not know
what a window is, and should not.

**Watch, and it came up during the build:** two stubs lagged the methods they
stood in for — a `create_approval` fake missing the new key, and earlier a
`FakeExchange` missing `request_digest`. The second made a cross-session test
pass while proving nothing. A fake missing a method the real one has does not
fail loudly; it makes the test measure something else.

---

## Decisions taken

All five were settled by the owner on 2026-09-18, and are recorded in the waves
above rather than only here.

1. **`operations.cancel` is a write**, and moves with the lifecycle scope.
2. **`propose` gets its own preset**, never folded into `analysis`.
3. **`visual` comes off the never-offered list.** It is necessary: without it an
   agent can propose but cannot attach evidence.
4. **The person driving the MCP session approves**, in that session, if their
   grant carries the right and they are satisfied with the evidence — not an
   operator at a separate dashboard. This is the decision the rest of wave 5
   hangs from, and the reason elicitation capability was measured before the
   design was written rather than after.

   *Reported unreachable on 2026-09-20 and restated by the owner the same day.*
   The transport could not carry a server-initiated request, so approval had
   fallen back to a dashboard over two calls. Honoured at wave 7 by enabling
   sessions, at the cost of `era_guard` obligation 4. The decision stands as
   written; what changed is the transport under it.
5. **An agent may drop a derived layer**, guarded by exact naming, a
   dependency check that refuses rather than warns, the dependent list carried
   into the approval, and a single-use receipt.

## Not in Phase 1

- `workspace get`, still. Nothing in the loop needs it.
- `validate`. `proposals.check` is the agent-shaped equivalent and costs a
  scope that means something.
- Federation mutation (`federation:register`, `federation:provision`) and
  semantic administration (`semantic:admin`, `semantic:generate`). Both are
  excluded from standing approval by the specification, both open outbound
  connections or spend an external provider, and neither belongs in the first
  release that can write anything.
- Retention and the audit read surface. Named as debt in
  `mcp-authorization.md`; a refused authorization is still unrecorded, and
  that is what makes retention required rather than optional.
