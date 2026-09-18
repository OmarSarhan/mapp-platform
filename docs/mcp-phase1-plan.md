# mapp-mcp Phase 1 — mutation, in waves

Phase 0 shipped an authorization broker, a request-bound token exchange and a
37-tool read surface, all merged and opt-in behind a Compose profile. Phase 1
gives an agent the other half of the platform's own loop: propose, review,
apply.

This plans the order. It is written to be argued with before any of it is
built, because two of the waves change what an existing scope means and one
adds a record type the specification has been reserving space for since P10.

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

**Decide first:** whether `operations.cancel` belongs with the lifecycle or
with reads. Cancelling someone else's job is a write, but an agent that can
start work arguably should be able to stop it.

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

**Decide first:** whether `propose` should be offered in a dashboard preset at
all, or only ever ticked individually. My recommendation is a separate
`authoring` preset, never folded into `analysis`.

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

**Watch for:** `visual` is in `NEVER_OFFERED_TO_AGENTS`. Like `semantic:source`
before it, that pin has to be revisited deliberately or this wave cannot ship.
The argument differs: a screenshot renders the workspace through a browser, so
it has a wider blast radius than a catalogue read.

## Wave 5 — approval intent and receipt

The substantial one, and the reason the four waves above come first: by here,
everything an agent can do is either reversible or reviewable by a person.

**Build:** the `control` records the specification has been reserving —
approval intent carrying the canonical execution digest and its bindings, and
a single-use receipt bound to that one digest, consumed atomically with the
effect. Plus the dashboard surface where an operator decides, and the audit
events for both decisions.

**This is where the envelope's three null members stop being null.**
`resolvedDefaults`, `confirmationFields` and `revisionBinding` were made
required arguments so that wiring them would be a visible edit at every call
site; this is that edit.

**Decide first, and it is the largest open question in Phase 1:** where the
operator decides. The dashboard is the only authenticated operator surface
that exists, and P8 requires a recently authenticated, CSRF-protected browser
session for standing windows — which implies per-action approval lives there
too. That makes an agent's mutation synchronous on a human at a browser, which
is correct and also the thing people will want to route around.

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

**Watch for:** `drop` is the first genuinely destructive tool on the surface.
It deserves its own decision about whether an agent should have it at all.

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

## Open decisions

These change the shape of the work, not just its order.

1. **Does `operations.cancel` belong with reads or with the lifecycle?** (Wave 1)
2. **Should `propose` ever appear in a preset?** (Wave 3)
3. **Does `visual` come off the never-offered list?** (Wave 4) — without this,
   agents can propose but cannot attach evidence.
4. **Where does an operator approve?** (Wave 5) — the largest one.
5. **Should an agent be able to `drop` a derived layer at all?** (Wave 7)

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
