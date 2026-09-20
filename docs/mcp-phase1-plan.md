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

**Not demonstrated end to end, and this is the honest gap.** Wave 5 builds the
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
