# Deployment

## Prerequisites

- A Linux host supported by Docker Engine.
- Docker Compose v2.
- Python 3 and OpenSSL for the wrapper's initialization and validation helpers.
- Sufficient persistent storage for PostgreSQL, Caddy data, audit/proposal
  records, and browser artifacts.
- Two distinct public DNS hostnames: one for the map and one for the
  configuration service.
- Direct public HTTPS on TCP port 443 to Caddy.
- A backup destination outside the deployment host.

The platform test command runs Node/frontend and component checks in
containers. A host Node installation and a repository-level npm install are
not prerequisites.

The normal deployment does not publish PostgreSQL. The optional
`compose.db-port.yaml` override is for deliberate, loopback-bound maintenance
access.

Production uses the hardening overlay:

```sh
export MAPP_ENVIRONMENT=production
./bin/mapp config
./bin/mapp serve
```

The wrapper includes `compose.production.yaml` for every command, validates
the production-only public settings, and forces secure dashboard cookies.
Keep `MAPP_ENVIRONMENT=production` set for all production `mapp` commands;
running the wrapper without it selects the development topology.

## Prepare the host

1. Clone the platform repository into a release-specific directory.
2. Review `instance/workspace.seed.json`, `instance/xyz.env`,
   `instance/etl/layers.json`, and public SVGs through normal version control.
   Confirm whether the duplicate-looking `Definitive Paths 2` seed layer is
   intentional before the first production release; do not silently remove it.
3. Run `./bin/mapp init` once. Record the displayed administrator password in
   an approved secret store.
4. Edit the private `.env` without committing it.
5. Run `./bin/mapp doctor` to report missing or obsolete environment keys
   without printing values.
6. Back up any existing database and `var` state before replacing a release.

The live `var` tree is operational state, not a deployment artifact. Preserve
it across application upgrades. For stricter host separation it may be placed
on dedicated persistent storage, but corresponding Compose mounts must be
changed and verified deliberately.

## Required production settings

At minimum, set and review:

- `PRODUCTION_MAP_SITE` and `PRODUCTION_CONFIG_SITE`: distinct public HTTPS
  origins using DNS hostnames and the standard port 443. IP literals,
  single-label/reserved names, trailing-dot names, and different ports on one
  hostname are rejected.
- `HTTPS_PORT=443`: required by the direct production topology.
- `EDGE_BIND_ADDRESS`: the intended host interface.
- `PRODUCTION_CADDY_EMAIL`: a monitored, non-placeholder ACME contact.
- `PRODUCTION_CONFIG_ALLOWED_HOSTS`: the deployed configuration hostname and
  only necessary internal names. Do not use a wildcard or trailing-dot name.
- `CONFIG_SECURE_COOKIES=true`.
- All PostgreSQL, ETL, and XYZ role passwords.
- `CONFIG_UID` and `CONFIG_GID`: the owner of writable live state.
- The pinned XYZ tag, commit, and accepted image identifier.

Do not put credentials in `instance`, the workspace, public SVGs, Compose
arguments, screenshots, or documentation.

The shipped production overlay models Caddy as the direct HTTPS endpoint. A
deployment behind another reverse proxy or on a non-standard public port is a
different topology and requires an explicit, reviewed change to validation,
trusted headers, binding, TLS, and health checks; do not bypass the validator.

## Start and load

```sh
export MAPP_ENVIRONMENT=production
./bin/mapp serve
./bin/mapp etl
./bin/mapp verify
```

`serve` starts the long-running services without changing database rows.
`etl` loads or refreshes configured sources. `verify` requires a running,
healthy stack with representative data and performs end-to-end checks.

Use `./bin/mapp ps` and `./bin/mapp logs` in the same exported production
environment while bringing up the release. Confirm both public hostnames,
authenticated dashboard access, the current workspace revision, XYZ reload
health, and a visual test for representative point, line, and polygon layers.
Also verify firewall exposure, certificate issuance/renewal, backup
restoration, and the browser runner's outbound asset policy. A reviewed egress
allowlist or local asset mirror remains recommended before long-term
production use.

## Remote client

Install the standalone `mapp-config-cli` on the operator or agent computer.
Create its bearer token in the dashboard, transfer the token through an
approved secret channel, initialize the production profile over HTTPS, and
delete any temporary token file after the credential has been stored securely.

Current bearer tokens have full workspace-configuration authority, including
proposal application, direct save, and reload. Dashboard password, token, and
audit administration remain admin-session-only. Until server-enforced
workspace scopes are implemented, issuing a token to an autonomous agent
grants more authority than the desired inspect-and-propose role.

## Upgrade and rollback

1. Take a fresh database and state backup.
2. Build and test the new images without altering the existing named database
   volume.
3. Record image digests and scan results when that release process is
   available.
4. Start the new release against staging or a restored copy of production
   state.
5. Run unit, Compose, integration, reload, and visual checks.
6. Recreate application containers for production.

Application rollback should restore the previous accepted images while
preserving PostgreSQL and `var`. Database schema or PostgreSQL major-version
changes require their own tested restore or migration plan; do not assume that
recreating an older container reverses them.
