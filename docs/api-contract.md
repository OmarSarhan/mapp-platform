# Configuration API contract

The configuration API is the integration boundary between MAPP Platform and
the separately released `mapp-config-cli`. The platform is authoritative for
workspace structure, rules, validation, revision handling, proposals, reloads,
and visual evidence.

## Discovery and versioning

`GET /api/public/identity` is public and returns the instance identifier,
authentication mode, contract version, and pinned XYZ version.

After bearer or dashboard-session authentication:

- `GET /api/contract` returns API, contract, rules, and XYZ versions, supported
  commands, workflow, and exit-code meanings.
- `GET /api/schema` returns the server workspace schema; a JSON Pointer may
  select a subsection.
- `GET /api/rules` returns validation and remediation rules, optionally by
  category.
- `GET /api/examples` returns server-owned examples.

The current versions are `1.0`, but a formal compatibility policy is still
required. The CLI should reject an unsupported major contract version and
should not assume that a newer command exists merely because an older server
used it.

Request bodies must be JSON objects of at most 5 MiB. Parsing is strict:
`NaN`, `Infinity`, and `-Infinity` are not JSON values and are rejected.
Responses are also emitted as strict JSON.

## Core reads

| Route | Purpose |
| --- | --- |
| `GET /api/workspace` | Workspace plus bytes-and-file-generation revision |
| `GET /api/catalog` | Database connections and renderable tables visible to XYZ |
| `GET /api/icons` | Valid public SVG choices |
| `GET /api/sql/capabilities` | Supported calculated-value expression model |
| `GET /api/proposals` | Proposal summaries |
| `GET /api/proposals/<id>` | Complete proposal record |
| `GET /api/xyz/status` | Requested/applied reload generations and health |
| `GET /api/artifacts/<path>` | Authenticated visual report or image |
| `GET /api/auth/me` | Current actor and reported scopes; session list for administrators |

## Mutations

Managed changes use a list of operations:

```json
[
  {
    "op": "set",
    "path": "/locale/layers/Bus Stops/display",
    "value": false
  }
]
```

Paths use strict RFC 6901 JSON Pointer escaping: `~0` represents `~` and `~1`
represents `/`; any other tilde escape is invalid. Object keys and zero-based
array indices are supported, but leading-zero, negative, missing, and
out-of-range array indices are rejected. `set` adds or replaces a value and
may append at the array length; `unset` removes an existing value. Replacing
or deleting the workspace root (the empty pointer) is not supported. The
pointer `/` is distinct: it addresses an object member whose key is empty.

Important routes include:

| Route | Purpose |
| --- | --- |
| `POST /api/validate` | Validate a complete candidate workspace |
| `POST /api/mutate` | Validate operations; save only when explicitly requested |
| `POST /api/proposals` | Create a revision-bound proposal lifecycle record |
| `POST /api/workspace` | Validate, save, and reload a complete workspace |
| `POST /api/proposals/<id>/apply` | Apply a pending proposal and request reload |
| `POST /api/proposals/<id>/decline` | Record rejection and optional reason |
| `POST /api/sql/test` | Probe one calculated information expression |
| `POST /api/visual-plan` | Choose a data-aware view, with optional named `locale` and bounded `centre`/`zoom` override |
| `POST /api/visual-test` | Run browser validation and create artifacts, with the same locale and view overrides |
| `POST /api/xyz/reload` | Request a generation-based XYZ reload |

Administrator-session routes create/revoke bearer tokens and change the
administrator password.

## Revisions and proposals

The revision is derived from the exact current workspace bytes and file
generation. A proposal records the original revision and applies only if that
revision is still current. A stale apply returns a conflict; clients must
inspect the new state and create a new proposal rather than silently rebasing.

Proposal records include complete original and candidate workspaces as well as
operations, focused diff, explanation, warnings, actor, timestamps, hashes,
and lifecycle status. Original intent and candidate content are retained while
the server updates status and apply/decline metadata. These are sensitive
operational records, not append-only files.

Application persists an internal `applying` transition before replacing the
workspace. If the service exits after the exact candidate commits, a repeated
apply of the same already-approved proposal can reconcile the candidate hash,
finish the `applied` record, and request reload again. If the workspace differs
from both the original revision and candidate, the proposal becomes
`conflicted` and must not be silently rebased. Clients may therefore observe
`pending`, `applying`, `applied`, `declined`, or `conflicted`; only the exact
approved proposal is eligible for recovery.

## Locale composition

The top-level `locale` is XYZ's default rendered locale, including when a
`locales` object is also present. XYZ pre-composes the top-level default into
each named locale except a named key literally called `locale`; that name
resolves the top-level default and is not a distinct alternative. If no locale
is requested, layer reads, SQL tests, and visual planning use the top-level
default. An explicit alternative name selects its effective composed locale.
If raw `workspace.locale` is absent, XYZ synthesizes the default as
`{"layers": {}}`. Both an omitted locale and the explicit name `locale`
select that empty default; the platform never auto-selects a sole named
alternative. Named alternatives other than `locale` compose over the
synthetic base.

Composition follows XYZ's special nested merge rules. Objects merge by key.
Arrays concatenate unless all source items are already present, in which case
the source array replaces the target; scalar values follow XYZ's source/target
precedence. Comma-separated locale composition uses the same framework rules.
The platform must not substitute a generic recursive merge. Mutations always
target the raw workspace JSON, so a focused operation must identify whether a
value is inherited from the default or owned by the named locale override.
Creating a real default where XYZ previously synthesized one requires adding
the raw `/locale` property explicitly.

Templates, external renderers, inline features, and zoom-keyed table or
geometry mappings are preserved and schema-validated. They are not forced
through a concrete database-relation probe when no single relation represents
their effective source.

## Authentication limitation

The current bearer-token records advertise only a full scope. The API does not
yet enforce separate inspect, propose, visual, apply, direct-write, or reload
permissions. Password changes, token administration, and audit access already
require an administrator session. Scoped and expiring bearer credentials are
required to make an AI-agent token meaningfully less privileged than an
approval token.

## Error handling

Clients should preserve the server's structured JSON error and use stable
non-zero exit codes for usage, validation, revision conflict, connectivity,
visual, and authentication failures. They must not print bearer tokens,
authorization headers, database URLs, or sensitive SQL samples.

Important status semantics are:

| Status | Meaning |
| ---: | --- |
| `400` | Malformed/oversized JSON object, invalid JSON Pointer, or invalid request value |
| `409` | Revision, proposal lifecycle, or integrity conflict |
| `422` | Candidate validation or browser validation did not pass |
| `429` | Login or visual-runner concurrency limit reached; retry later |
| `504` | The workspace/proposal write may already be committed, but XYZ reload confirmation did not complete |

A browser-validation `422` retains the selected plan and the browser runner's
failed report and artifact paths. Clients should surface that evidence rather
than replace it with a generic error.

The browser runner allows one active test by default and is hard-clamped to
1–4. When it is full, `/api/visual-test` propagates HTTP 429 with the selected
plan and retryable error instead of converting the condition to an upstream
failure.

For direct workspace or saved-mutation responses, `saved: true` in a `504`
means the new revision and fingerprint were committed. For proposal apply, an
`applied` proposal with `appliedRevision` is the committed-state signal. Do not
blindly retry either result: read the proposal, workspace revision, and XYZ
status first. Dry-run mutation must return `saved: false`; a client should fail
closed if a server does not make that guarantee.

## Compatibility testing

Every platform/CLI release pair should test discovery, schema/rules, workspace
reads, dry-run mutation, validation failure, proposal lifecycle, stale
revision, reload status, visual results, authentication failure, and redaction.
The client should use the server's contract rather than duplicating evolving
workspace rules.
