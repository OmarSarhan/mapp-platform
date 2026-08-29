# MAPP Platform

MAPP publishes maps from PostgreSQL data that lives somewhere else.

You point it at PostgreSQL databases you already have. It attaches them
read-only, records what it knows about their relations, lets you build
aggregated layers across them, and serves the result through a pinned GEOLYTIX
XYZ build — with a configuration dashboard, a private semantic metadata
service, server-side browser validation, and Caddy in front.

**MAPP does not hold your spatial data.** It packages one PostgreSQL database
for its own state: the layers it derives, the registry of sources it has
attached, and what it knows about their columns. Your data stays where it is.

---

## Try it

Docker with Compose, and about 4 GB free. Nothing is installed on the host.

```sh
./bin/mapp init --demo     # write .env and generate every secret
./bin/mapp all             # build, start, and verify the platform
./bin/mapp demo            # load two real open-data sources and publish a map
```

`demo` takes around fifteen minutes, most of it downloading the England Census
2021 Output Area dataset. Then open:

- the map — <http://localhost:3000>
- the dashboard — <http://config.localhost:3000>

The dashboard password was printed by `init`; `./bin/mapp reset-config-password`
issues a new one if you have lost it.

What you get is two independent PostgreSQL servers holding real open data,
attached read-only, with layers computed by joining across both of them.

**Next: [the guide](docs/guide.md).** It starts here and builds up to attaching
your own sources, so it is the right second thing to read.

## Without the demo

```sh
./bin/mapp init
./bin/mapp all
```

That gives you the platform and nothing in it. Attach your own PostgreSQL
database with the [`config-cli`](https://github.com/OmarSarhan/mapp-config-cli)
client, or through the dashboard's federation panel — the guide's
[federation section](docs/guide.md#4-federation-attaching-a-source) walks the
lifecycle, and [`docs/external-postgresql.md`](docs/external-postgresql.md)
covers preparing the source database itself.

## Common commands

| Command | What it does |
| --- | --- |
| `./bin/mapp all` | Start everything, then verify it |
| `./bin/mapp serve` | Start the long-running services and load nothing |
| `./bin/mapp demo` | Load the two demo sources and publish their map layers |
| `./bin/mapp verify` | End-to-end acceptance checks against the running stack |
| `./bin/mapp test` | Unit, contract and frontend suites |
| `./bin/mapp doctor` | Report `.env` key drift; `--add-missing` fills safe defaults |
| `./bin/mapp ps`, `logs` | Service state and logs |
| `./bin/mapp stop`, `down` | Stop, or remove containers and keep the data |
| `./bin/mapp reset-data --confirm` | Remove the packaged database; read the warning first |

`./bin/mapp` with no arguments lists all of them.

## Layout

| Path | Contents |
| --- | --- |
| `bin/mapp` | The wrapper every operation goes through |
| `config-ui/` | Configuration dashboard and API |
| `semantic-service/` | Private semantic catalogue service |
| `etl/` | Loader used by the demo to populate source databases |
| `docker/` | Image definitions and database initialisation |
| `scripts/` | `verify.sh`, acceptance and contract test helpers |
| `instance/` | Reviewed, versioned inputs — seed workspace, public assets |
| `var/` | Runtime state: authentication, audit, proposals, artifacts |
| `docs/` | [Documentation](docs/guide.md) |

`instance/` is checked in and reviewed. `var/` is generated, private, and
excluded from Git.

## Documentation

**[docs/guide.md](docs/guide.md)** is the entry point. It explains the mental
model, walks the first twenty minutes, and adds depth as it goes, linking to
every reference document in the right place.

If you already know what you are looking for, its
[reference index](docs/guide.md#10-reference-index) lists all of them with a
line each.

## The remote client

[`config-cli`](https://github.com/OmarSarhan/mapp-config-cli) is a separate
repository. It is installed on an operator or AI-agent computer and reaches
this platform only through the authenticated configuration API — it is not
bundled into any image here. That separation is the trust boundary, so keep it.

## Contributing

[`CONTRIBUTING.md`](CONTRIBUTING.md) covers the development container, the
suites, and what a change is expected to carry before it lands.
