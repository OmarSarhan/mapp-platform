from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_BASES = {
    ".devcontainer/Dockerfile": (
        "node:22.23.1-bookworm-slim@sha256:"
        "6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3",
        "mcr.microsoft.com/devcontainers/python:1-3.12-bookworm@sha256:"
        "7876580dc67fd460fd962f004cbeb480027e9bbc0657096f1087db11f9eaff39",
    ),
    "browser-runner/Dockerfile": (
        "mcr.microsoft.com/playwright:v1.61.1-noble@sha256:"
        "5b8f294aff9041b7191c34a4bab3ac270157a28774d4b0660e9743297b697e48",
    ),
    "docker/caddy/Dockerfile": (
        "golang:1.26.5-alpine3.23@sha256:"
        "622e56dbc11a8cfe87cafa2331e9a201877271cbff918af53d3be315f3da88cc",
        "caddy:2.11.4-alpine@sha256:"
        "5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648",
    ),
    "config-ui/Dockerfile": (
        "node:22.23.1-alpine3.23@sha256:"
        "8516dce0483394d5708d4b2ee6cacb79fb1d617ea4e2787c2120bcca92ce372e",
        "python:3.12.13-alpine3.23@sha256:"
        "601d3d3797e90e2534782e69c85fafb7971b43f24c7b1b079b7e48dd435e458d",
    ),
    "docker/egress-proxy/Dockerfile": (
        "ubuntu/squid:6.6-24.04_edge@sha256:"
        "8a3baed477e2c282ab8aa5edad442f69873246964f225c5c2ae8364b6610963c",
    ),
    "docker/postgis/Dockerfile": (
        "golang:1.26.5-alpine3.23@sha256:"
        "622e56dbc11a8cfe87cafa2331e9a201877271cbff918af53d3be315f3da88cc",
        "postgis/postgis:17-3.5-alpine@sha256:"
        "978a2e6671c956d650d1f240dba7c73b8519a5f5af8685165fca616cc4ae3568",
    ),
    "docker/xyz/Dockerfile": (
        "node:22.23.1-alpine3.23@sha256:"
        "8516dce0483394d5708d4b2ee6cacb79fb1d617ea4e2787c2120bcca92ce372e",
        "node:22.23.1-alpine3.23@sha256:"
        "8516dce0483394d5708d4b2ee6cacb79fb1d617ea4e2787c2120bcca92ce372e",
    ),
    "etl/Dockerfile": (
        "python:3.12.13-alpine3.23@sha256:"
        "601d3d3797e90e2534782e69c85fafb7971b43f24c7b1b079b7e48dd435e458d",
    ),
    "semantic-service/Dockerfile": (
        "python:3.12.13-alpine3.23@sha256:"
        "601d3d3797e90e2534782e69c85fafb7971b43f24c7b1b079b7e48dd435e458d",
    ),
}


class BaseImagePolicyTests(unittest.TestCase):
    def test_every_dockerfile_uses_the_reviewed_digest(self) -> None:
        discovered = {
            path.relative_to(REPOSITORY_ROOT).as_posix()
            for path in REPOSITORY_ROOT.rglob("Dockerfile")
            if ".git" not in path.parts
        }
        self.assertEqual(discovered, set(EXPECTED_BASES))

        for relative_path, expected in EXPECTED_BASES.items():
            lines = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8").splitlines()
            actual = tuple(
                line.split()[1]
                for line in lines
                if line.strip().upper().startswith("FROM ")
            )
            self.assertEqual(actual, expected, relative_path)
            for reference in actual:
                self.assertRegex(reference, r"^[^@$\s]+@sha256:[0-9a-f]{64}$")

    def test_external_dockerfile_frontend_is_digest_pinned(self) -> None:
        xyz = (REPOSITORY_ROOT / "docker/xyz/Dockerfile").read_text(encoding="utf-8")
        self.assertTrue(
            xyz.startswith(
                "# syntax=docker/dockerfile:1.7@sha256:"
                "a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e\n"
            )
        )

    def test_runtime_vulnerability_remediation_is_preserved(self) -> None:
        xyz = (REPOSITORY_ROOT / "docker/xyz/Dockerfile").read_text(
            encoding="utf-8",
        )
        browser = (REPOSITORY_ROOT / "browser-runner/Dockerfile").read_text(
            encoding="utf-8",
        )
        egress = (REPOSITORY_ROOT / "docker/egress-proxy/Dockerfile").read_text(
            encoding="utf-8",
        )
        caddy = (REPOSITORY_ROOT / "docker/caddy/Dockerfile").read_text(
            encoding="utf-8",
        )
        postgis = (REPOSITORY_ROOT / "docker/postgis/Dockerfile").read_text(
            encoding="utf-8",
        )

        self.assertIn("/usr/local/lib/node_modules/npm", xyz)
        self.assertIn("gstreamer1.0-plugins-bad", browser)
        self.assertIn("/usr/lib/node_modules/npm", browser)
        self.assertIn("--only-upgrade", egress)
        self.assertIn("libssl3t64=3.0.13-0ubuntu3.12", egress)
        self.assertIn("openssl=3.0.13-0ubuntu3.12", egress)
        self.assertIn(
            "CADDY_COMMIT=e2eee6a7fce366321294c9c2a79f3146891dcbdf",
            caddy,
        )
        self.assertIn('"refs/tags/${CADDY_VERSION}"', caddy)
        self.assertIn("'FETCH_HEAD^{commit}'", caddy)
        self.assertIn(
            'go mod edit -replace="github.com/caddyserver/caddy/v2=/reviewed-source"',
            caddy,
        )
        self.assertIn("golang.org/x/text@v0.39.0", caddy)
        self.assertIn("google.golang.org/grpc@v1.82.1", caddy)
        self.assertIn("c-ares=1.34.8-r0", caddy)
        self.assertIn("curl=8.20.0-r0", caddy)
        self.assertIn("libcurl=8.20.0-r0", caddy)
        self.assertIn(
            "GOSU_COMMIT=6456aaa0f3c854d199d0f037f068eb97515b7513",
            postgis,
        )
        self.assertIn(
            "COPY --from=gosu-builder /out/gosu /usr/local/bin/gosu",
            postgis,
        )
        self.assertIn("build-base=0.5-r4", postgis)
        self.assertIn("cmake=4.2.3-r0", postgis)
        self.assertIn("git=2.54.0-r0", postgis)
        self.assertIn("postgresql17-dev=17.11-r0", postgis)


class SupplyChainWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = (
            REPOSITORY_ROOT / ".github/workflows/supply-chain.yml"
        ).read_text(encoding="utf-8")
        matrix_match = re.search(
            r"          MATRIX_JSON: >-\n"
            r"(?P<body>(?:            .*\n)+)"
            r"        run:",
            cls.workflow,
        )
        if matrix_match is None:
            raise AssertionError("supply-chain matrix JSON is missing")
        cls.matrix = json.loads(
            " ".join(line.strip() for line in matrix_match.group("body").splitlines())
        )
        jobs_text = cls.workflow.split("\njobs:\n", maxsplit=1)[1]
        matches = list(re.finditer(r"^  ([a-z][a-z0-9-]*):\n", jobs_text, re.MULTILINE))
        cls.jobs = {
            match.group(1): jobs_text[
                match.start() : matches[index + 1].start()
                if index + 1 < len(matches)
                else len(jobs_text)
            ]
            for index, match in enumerate(matches)
        }

    def test_actions_are_full_commit_sha_pinned(self) -> None:
        uses = re.findall(r"^\s*uses:\s*([^@\s]+)@([^\s#]+)", self.workflow, re.MULTILINE)
        self.assertTrue(uses)
        for action, revision in uses:
            self.assertRegex(revision, r"^[0-9a-f]{40}$", action)

    def test_every_final_image_is_built(self) -> None:
        dockerfiles = [entry["dockerfile"] for entry in self.matrix["include"]]
        expected = set(EXPECTED_BASES) - {".devcontainer/Dockerfile"}
        self.assertEqual(set(dockerfiles), expected)
        self.assertEqual(len(dockerfiles), len(expected))

    def test_image_matrix_is_compacted_before_export(self) -> None:
        plan = self.jobs["plan"]
        self.assertIn('matrix="$(jq -c . <<<"$MATRIX_JSON")"', plan)
        self.assertIn(
            "printf 'matrix=%s\\n' \"$matrix\" >> \"$GITHUB_OUTPUT\"",
            plan,
        )
        self.assertNotIn(
            "printf 'matrix=%s\\n' \"$MATRIX_JSON\" >> \"$GITHUB_OUTPUT\"",
            plan,
        )

    def test_scanners_have_no_publish_or_oidc_authority(self) -> None:
        self.assertEqual(set(self.jobs), {"plan", "build", "scan", "sign"})

        build = self.jobs["build"]
        self.assertIn("packages: write", build)
        self.assertNotIn("id-token: write", build)
        self.assertNotIn("anchore/sbom-action", build)
        self.assertNotIn("aquasecurity/trivy-action", build)
        self.assertNotIn("sigstore/cosign-installer", build)

        scan = self.jobs["scan"]
        self.assertIn("packages: read", scan)
        self.assertNotIn("packages: write", scan)
        self.assertNotIn("id-token: write", scan)
        self.assertIn("anchore/sbom-action", scan)
        self.assertIn("aquasecurity/trivy-action", scan)
        self.assertIn("version: v0.73.0", scan)
        self.assertNotIn("sigstore/cosign-installer", scan)

        sign = self.jobs["sign"]
        self.assertIn("packages: write", sign)
        self.assertIn("id-token: write", sign)
        self.assertNotIn("anchore/sbom-action", sign)
        self.assertNotIn("aquasecurity/trivy-action", sign)
        self.assertIn("needs: [plan, scan]", sign)
        self.assertLess(sign.index("cosign sign-blob"), sign.index("Retain signature bundles"))
        self.assertLess(sign.index("Retain signature bundles"), sign.index("Sign the image last"))

    def test_compose_deploys_the_reviewed_caddy_wrapper(self) -> None:
        compose = (REPOSITORY_ROOT / "compose.yaml").read_text(encoding="utf-8")
        example_env = (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("image: mapp-caddy:2.11.4", compose)
        self.assertRegex(
            compose,
            r"(?ms)^  caddy:\n.*?^    build:\n"
            r"      context: \./docker/caddy\n"
            r"      dockerfile: Dockerfile$",
        )
        self.assertNotIn("CADDY_IMAGE=", example_env)

    def test_release_requires_oidc_and_never_reads_a_signing_key(self) -> None:
        self.assertIn("id-token: write", self.workflow)
        self.assertIn("cosign sign --yes", self.workflow)
        self.assertIn("github.ref == 'refs/heads/main'", self.workflow)
        self.assertNotRegex(self.workflow, r"COSIGN_(?:PRIVATE_KEY|PASSWORD)")
        self.assertNotRegex(self.workflow, r"cosign (?:sign|sign-blob)[^\n]*--key")
        referenced_secrets = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", self.workflow))
        self.assertEqual(referenced_secrets, {"GITHUB_TOKEN"})

    def test_digest_bound_evidence_and_policy_are_retained(self) -> None:
        required_fragments = (
            "IMAGE_DIGEST: ${{ steps.build.outputs.digest }}",
            "image-ref=%s@%s",
            "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
            "subject-${{ matrix.image }}-${{ github.sha }}-${{ github.run_id }}",
            "provenance: mode=max",
            "sbom: generator=docker/buildkit-syft-scanner:stable-1@sha256:79e7b013cbec16bbb436f312819a49a4a57752b2270c1a9332ae1a10fcc82a68",
            "DOCKER_BUILD_RECORD_RETENTION_DAYS: 30",
            "version: v0.34.1",
            "image=moby/buildkit:v0.30.0@sha256:0168606be2315b7c807a03b3d8aa79beefdb31c98740cebdffdfeebf31190c9f",
            "spdx-json=",
            "cyclonedx-json=",
            "Provenance.SLSA",
            "type == \"object\" and length > 0",
            "severity: HIGH,CRITICAL",
            "limit-severities-for-sarif: true",
            'exit-code: "1"',
            "push: true",
            "provenance.sigstore.json",
            "cosign verify",
            "retention-days: 30",
        )
        for fragment in required_fragments:
            self.assertIn(fragment, self.workflow)

        artifact_names = re.findall(
            r"^          name: (?:subject|evidence|signatures)-.*$",
            self.workflow,
            re.MULTILINE,
        )
        self.assertTrue(artifact_names)
        self.assertTrue(
            all("run_attempt" not in name for name in artifact_names)
        )
        self.assertGreaterEqual(self.workflow.count("overwrite: true"), 3)

    def test_private_personal_repository_does_not_use_enterprise_attestations(self) -> None:
        self.assertNotRegex(
            self.workflow,
            r"actions/attest(?:-build-provenance)?@",
        )
        self.assertNotIn("attestations: write", self.workflow)
        self.assertNotIn("artifact-metadata: write", self.workflow)


if __name__ == "__main__":
    unittest.main()
