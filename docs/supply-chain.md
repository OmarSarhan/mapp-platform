# Container supply-chain policy

MAPP images are built only from reviewed, immutable base-image digests. The
human-readable tags remain beside the digests so an update can be reviewed as
an explicit tag-and-digest change. A digest update is a security change: inspect
the publisher, architectures, release notes, and vulnerability delta before
merging it.

## Reviewed bases

The following multi-platform index digests were resolved on 2026-08-04 with
`docker buildx imagetools inspect IMAGE:TAG`. The static supply-chain test is the
machine-readable allowlist and must change in the same review as a Dockerfile.

| Base | Reviewed index digest | Used by |
| --- | --- | --- |
| `node:22.23.1-bookworm-slim` | `sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3` | XYZ build/runtime and config UI web build |
| `python:3.12.13-slim-bookworm` | `sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b` | config UI, semantic service, and ETL |
| `postgis/postgis:17-3.5` | `sha256:45f2a608397fa67d236b012c14a9e3ea31e9fe813edbeb5c1c0d1acbf0d48ea9` | bundled PostgreSQL/PostGIS/H3 image |
| `mcr.microsoft.com/playwright:v1.61.1-noble` | `sha256:5b8f294aff9041b7191c34a4bab3ac270157a28774d4b0660e9743297b697e48` | browser runner |
| `caddy:2.10.0-alpine` | `sha256:ae4458638da8e1a91aafffb231c5f8778e964bca650c8a8cb23a7e8ac557aa3c` | Caddy edge wrapper |
| `ubuntu/squid:6.6-24.04_edge` | `sha256:8a3baed477e2c282ab8aa5edad442f69873246964f225c5c2ae8364b6610963c` | allowlisting egress proxy |
| `mcr.microsoft.com/devcontainers/python:1-3.12-bookworm` | `sha256:7876580dc67fd460fd962f004cbeb480027e9bbc0657096f1087db11f9eaff39` | development container only |

XYZ's external `docker/dockerfile:1.7` build frontend is also pinned to index
digest `sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e`.

Canonical publishes `ubuntu/squid` as a Verified Publisher image. The selected
Squid 6.6 / Ubuntu 24.04 line is supported through May 2029. Its configuration
is mounted read-only at `/etc/squid/squid.conf`, it listens on port 3128, and
its optional log/cache paths are `/var/log/squid` and `/var/spool/squid`.
Caddy's wrapper adds nothing to the reviewed upstream image; Compose continues
to mount the same Caddyfile and retains the upstream entrypoint and command.

## Publication workflow

`.github/workflows/supply-chain.yml` runs only for `main` (including manual
runs selected on `main`). It publishes one `linux/amd64` GHCR image for each of
the eight final Dockerfiles, including the Caddy wrapper, under a unique
source/run tag. Deployment and verification must use the emitted image digest
rather than that tag.

For every image digest the workflow:

1. adds BuildKit maximum-mode SLSA provenance and an OCI SBOM attestation;
2. retains the attached SLSA provenance and generates SPDX JSON and CycloneDX
   JSON SBOMs with Syft 1.44.0;
3. scans OS and application packages with Trivy 0.70.0 and fails on every High
   or Critical finding, including findings without an available fix;
4. signs the image, both retained SBOMs, and retained provenance keylessly with
   Cosign 3.0.6; and
5. verifies the Cosign certificate identity and OIDC issuer before completion.

All actions use full commit SHAs. The build, scan, and signing phases run as
separate jobs. The build job can read source and publish packages but cannot
request an OIDC identity. The scan job gets only `packages: read`, does not
check out source, and removes its read-only registry credential after use. Only
the signing job gets `packages: write` and `id-token: write`; it contains no
scanner action. The immutable image name and digest cross these boundaries in
a 30-day subject artifact, and signing starts only after the complete scan
matrix succeeds. Evidence blobs are signed, verified, and retained before the
image is signed, so an evidence failure cannot leave a release-looking image
signature behind.

Signing uses the GitHub Actions OIDC identity; there is no repository signing
key or Cosign password. Registry login uses only the job-scoped
`GITHUB_TOKEN`. The release builder is also fixed to Buildx 0.34.1 and the
multi-platform BuildKit 0.30.0 index digest
`sha256:0168606be2315b7c807a03b3d8aa79beefdb31c98740cebdffdfeebf31190c9f`.
BuildKit's `docker/buildkit-syft-scanner:stable-1` generator is fixed to index
digest
`sha256:79e7b013cbec16bbb436f312819a49a4a57752b2270c1a9332ae1a10fcc82a68`.

The BuildKit OCI attestations and Cosign image signature remain attached to the
image in GHCR. Separate subject, scan-evidence, and signature-bundle artifacts,
plus the BuildKit build record, are retained for 30 days. A failed scan still
leaves the SBOM, provenance, and scan evidence available for review, but no
signing job starts unless every final-image scan succeeds.

For an image named `IMAGE` and digest `DIGEST`, consumers should verify both
identities before deployment:

```sh
cosign verify \
  --certificate-identity \
  "https://github.com/OWNER/REPOSITORY/.github/workflows/supply-chain.yml@refs/heads/main" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
  "IMAGE@DIGEST"

docker buildx imagetools inspect "IMAGE@DIGEST" \
  --format '{{ json .Provenance.SLSA }}'
```

Do not loosen the certificate identity to accept arbitrary branches or
workflows. A release/deployment policy should also require the expected GitHub
repository and `main` workflow identity, not merely the presence of any valid
signature.

Promotion must require both a successful supply-chain workflow conclusion and
the expected image signature. A signature alone is not a release marker: an
external failure while verifying the final signature could still make the job
fail after the registry accepted it.

To deploy the published Caddy wrapper instead of building it locally, use a
deployment-owned Compose override with the verified digest; do not restore the
old `CADDY_IMAGE` variable because a digest cannot also be a build output tag:

```yaml
services:
  caddy:
    image: ghcr.io/OWNER/REPOSITORY-caddy@sha256:DIGEST
    build: !reset null
```

Apply that file after the repository Compose files, pull the digest, and start
with `--no-build`. Keep the override outside source control if it contains
environment-specific release selection.

Registry-native attestations require the candidate image to be pushed before
the scan. A rejected candidate therefore remains under its unique run tag but
is never signed; production policy must reject every unsigned digest.

## Updating a base or tool

1. Choose a supported upstream tag and review its publisher and release notes.
2. Resolve the index digest with `docker buildx imagetools inspect IMAGE:TAG`.
3. Review the platform manifests and scan the candidate base.
4. Update every matching Dockerfile, the table above, and
   `scripts/tests/test_supply_chain.py` in one pull request.
5. Pin workflow action updates to the release commit rather than a mutable tag,
   and keep the Syft, Trivy, and Cosign versions explicit.
6. Run the helper tests and build the affected final image before merge. Review
   the first `main` workflow evidence before promoting its digest.

Digest pinning intentionally stops silent upstream security updates. Schedule
reviewed digest refreshes, and treat scanner findings or publisher security
notices as triggers for an immediate refresh.

## Limitations

- The PostGIS base currently publishes only `linux/amd64`, so the release
  workflow is intentionally single-platform.
- A pinned base does not make all package downloads reproducible. Debian/Ubuntu
  package indexes and any unlocked language dependency remain time-dependent;
  the unique run tag and final digest prevent that difference from being hidden.
- Vulnerability results change as Trivy's advisory database changes. The
  scanner version and immutable image subject are pinned, while the retained
  SARIF records the result seen by that run.
- Keyless Cosign uses Sigstore services. Their availability is an external
  release dependency, and public transparency-log entries disclose the signing
  workflow identity and artifact digest.
- GitHub Artifact Attestations are not enabled because this is a private,
  personal-account repository; GitHub requires Enterprise Cloud for private
  repository attestations. If the project moves to an Enterprise Cloud
  organization, that repository-native record can be added as a separate
  reviewed control without replacing the BuildKit provenance.
- Build provenance links an artifact to its source and build; it is not proof
  that the source is safe. High/Critical scan failures, code review, and
  signature verification remain separate required controls.

## Design sources

- Docker: [pin base images to digests](https://docs.docker.com/build/building/best-practices/#pin-base-image-versions),
  [add SBOM/provenance attestations](https://docs.docker.com/build/ci/github-actions/attestations/),
  [inspect SLSA provenance](https://docs.docker.com/build/metadata/attestations/slsa-provenance/#inspecting-provenance),
  and [reset an inherited Compose value](https://docs.docker.com/reference/compose-file/merge/#reset-value).
- GitHub: [pin actions to full commit SHAs](https://docs.github.com/en/actions/how-tos/create-and-publish-actions/manage-custom-actions#using-release-management-for-actions)
  and [set least-privilege `GITHUB_TOKEN` permissions](https://docs.github.com/en/actions/security-for-github-actions/security-guides/automatic-token-authentication#modifying-the-permissions-for-the-github_token).
- Anchore: [generate SPDX and CycloneDX SBOMs with Syft](https://github.com/anchore/syft#readme).
- Sigstore: [keyless CI signing with Cosign](https://docs.sigstore.dev/quickstart/quickstart-ci/)
  and [identity-bound verification](https://docs.sigstore.dev/cosign/verifying/verify/).
- OCI: [Image and Distribution 1.1 referrers](https://opencontainers.org/posts/blog/2024-03-13-image-and-distribution-1-1/)
  for signatures, SBOMs, and provenance attached to an image digest.
- Canonical: [the maintained `ubuntu/squid` image](https://hub.docker.com/r/ubuntu/squid).
- Docker Official Images: [the maintained Caddy image](https://hub.docker.com/_/caddy).
