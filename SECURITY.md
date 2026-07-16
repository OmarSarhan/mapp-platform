# Security policy

## Supported versions

The project does not yet publish formal releases or a supported-version
matrix. Until that exists, only the current owner-designated deployment should
be treated as a candidate for security fixes.

## Reporting a vulnerability

Report suspected vulnerabilities privately to the repository owner or the
private security contact configured for the eventual hosting service. Do not
open a public issue containing:

- credentials, bearer tokens, cookies, hashes, or authorization headers;
- database URLs or private hostnames;
- confidential SQL expressions or feature data;
- audit records, proposal contents, screenshots, or backup archives;
- a working exploit against a live instance.

Include the affected component and version, prerequisites, impact, minimal
reproduction, and any safe mitigation. Remove or redact secrets before sending
logs.

The owner should acknowledge the report, establish a private reproduction,
rotate exposed credentials immediately, and coordinate remediation and
disclosure. No response-time commitment is declared yet.

## Operational guidance

See [`docs/security.md`](docs/security.md) for the threat model, filesystem and
network boundaries, current full-scope token limitation, SQL/SVG controls, and
the outstanding need for isolated proposal previews.
