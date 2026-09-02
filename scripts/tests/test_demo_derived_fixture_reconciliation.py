from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LAYERS_SCRIPT = ROOT / "docker" / "demo-sources" / "layers.sh"
FIXTURES = ROOT / "docker" / "demo-sources" / "derived-layers"


def embedded_program(function_name: str) -> str:
    source = LAYERS_SCRIPT.read_text(encoding="utf-8")
    function = source.index(f"{function_name}()")
    start = source.index("<<'PY'\n", function) + len("<<'PY'\n")
    end = source.index("\nPY\n", start)
    return source[start:end]


def run_embedded_program(
    function_name: str,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-", *arguments],
        input=embedded_program(function_name),
        capture_output=True,
        text=True,
        check=False,
    )


def fixture_definition(manifest: Path) -> dict:
    result = run_embedded_program("fixture_definition", str(manifest))
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def decision(
    manifest: Path,
    response: dict,
    desired: dict,
) -> subprocess.CompletedProcess[str]:
    return run_embedded_program(
        "fixture_decision",
        str(manifest),
        json.dumps(response),
        json.dumps(desired),
    )


class DemoDerivedFixtureReconciliationTests(unittest.TestCase):
    manifest = FIXTURES / "census-oa-population-quintiles.json"

    def setUp(self) -> None:
        self.desired = fixture_definition(self.manifest)
        self.desired["spatialScope"] = {
            "type": "workspace-map-extent",
            "locale": "locale",
            "sourceView": {"lng": -1.5, "lat": 53.8, "z": 11},
            "scopeZoom": 10,
            "zoomOffset": -1,
            "viewport": {"width": 1280, "height": 720},
            "crs": "EPSG:4326",
            "envelopes": [
                {"west": -1.85, "south": 53.65, "east": -1.2, "north": 54}
            ],
            "selection": "configured-locale-extent",
            "clipsGeometry": True,
            "guidance": [],
        }

    def test_loader_strips_local_manifest_metadata(self) -> None:
        definition = fixture_definition(self.manifest)

        self.assertNotIn("queryFile", definition)
        self.assertNotIn("legacyQuerySha256", definition)
        self.assertTrue(definition["query"].startswith("WITH scoped AS"))

    def test_missing_definition_is_created(self) -> None:
        result = decision(
            self.manifest,
            {"code": "derived_layer.not_found"},
            self.desired,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("create", result.stdout.strip())

    def test_exact_definition_is_left_unchanged(self) -> None:
        result = decision(
            self.manifest,
            {"derivedLayer": self.desired},
            self.desired,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("same", result.stdout.strip())

    def test_exact_query_without_owner_description_is_adopted(self) -> None:
        current = {**self.desired, "description": ""}

        result = decision(
            self.manifest,
            {"derivedLayer": current},
            self.desired,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("replace", result.stdout.strip())

    def test_allowlisted_legacy_query_is_adopted(self) -> None:
        legacy_query = "SELECT legacy"
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = Path(temporary) / "fixture.json"
            manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
            manifest["legacyQuerySha256"] = [
                hashlib.sha256(legacy_query.encode()).hexdigest()
            ]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            current = {**self.desired, "query": legacy_query, "description": ""}

            result = decision(
                manifest_path,
                {"derivedLayer": current},
                self.desired,
            )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("replace", result.stdout.strip())

    def test_same_name_foreign_query_is_refused(self) -> None:
        current = {**self.desired, "query": "SELECT unrelated"}

        result = decision(
            self.manifest,
            {"derivedLayer": current},
            self.desired,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("not owned by this demo", result.stderr)

    def test_resolved_scope_mismatch_is_refused(self) -> None:
        current = json.loads(json.dumps(self.desired))
        current["spatialScope"]["envelopes"][0]["east"] = -1.1

        result = decision(
            self.manifest,
            {"derivedLayer": current},
            self.desired,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("not owned by this demo", result.stderr)

    def test_backend_failure_is_not_treated_as_absence(self) -> None:
        result = decision(
            self.manifest,
            {"code": "derived_layer.read_unavailable"},
            self.desired,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("lookup failed", result.stderr)


class DemoLayersLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = LAYERS_SCRIPT.read_text(encoding="utf-8")

    def function_source(self, name: str, next_name: str) -> str:
        start = self.source.index(f"{name}()")
        end = self.source.index(f"\n{next_name}()", start)
        return self.source[start:end]

    def test_demo_token_outlives_the_bounded_background_work(self) -> None:
        self.assertIn("dt.timedelta(hours=24)", self.source)
        self.assertNotIn("dt.timedelta(minutes=45)", self.source)

    def test_capacity_parser_distinguishes_full_and_available_queues(self) -> None:
        for active, maximum, expected in (
            (0, 2, "available\t0\t2"),
            (1, 2, "available\t1\t2"),
            (2, 2, "full\t2\t2"),
        ):
            with self.subTest(active=active, maximum=maximum):
                result = run_embedded_program(
                    "derived_capacity_state",
                    json.dumps({
                        "backgroundJobs": {
                            "activeJobs": active,
                            "maxActiveJobs": maximum,
                        }
                    }),
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(expected, result.stdout.strip())

        invalid = run_embedded_program(
            "derived_capacity_state",
            json.dumps({
                "backgroundJobs": {
                    "activeJobs": True,
                    "maxActiveJobs": 2,
                }
            }),
        )
        self.assertNotEqual(0, invalid.returncode)
        self.assertIn("counts are invalid", invalid.stderr)

    def test_capacity_is_checked_before_post_with_429_race_recovery(self) -> None:
        function = self.function_source(
            "background_derived_mutation",
            "validate_xyz_reload",
        )

        capacity_get = function.index(
            "api GET /api/derived-layers/background-jobs"
        )
        mutation_post = function.index("curl -sS -X POST")
        self.assertLess(capacity_get, mutation_post)
        self.assertIn('"${capacity_state}" = "full"', function)
        self.assertIn('"${http_status}" = "429"', function)
        self.assertIn(
            '"${response_code}" = "derived_layer.background_capacity"',
            function,
        )

    def test_cancelling_background_operation_remains_nonterminal(self) -> None:
        function = self.function_source(
            "background_derived_mutation",
            "validate_xyz_reload",
        )

        self.assertIn("running|cancelling) sleep 2 ;;", function)
        terminal = function[function.index("failed|cancelled|indeterminate"):]
        self.assertNotIn("cancelling", terminal)

    def test_xyz_reload_must_confirm_the_bound_fingerprint(self) -> None:
        fingerprint = "c" * 64
        response = {
            "requestedGeneration": 9,
            "expectedWorkspaceFingerprint": fingerprint,
            "status": {
                "appliedGeneration": 9,
                "workspaceFingerprint": fingerprint,
                "healthy": True,
                "completed": True,
            },
        }
        valid = run_embedded_program(
            "validate_xyz_reload",
            json.dumps(response),
        )
        self.assertEqual(0, valid.returncode, valid.stderr)

        invalid = run_embedded_program(
            "validate_xyz_reload",
            json.dumps({
                **response,
                "status": {
                    **response["status"],
                    "workspaceFingerprint": "d" * 64,
                },
            }),
        )
        self.assertNotEqual(0, invalid.returncode)
        self.assertIn("did not load", invalid.stderr)

    def test_exact_workspace_path_uses_supported_bound_reload_api(self) -> None:
        function = self.function_source(
            "ensure_saved_workspace_xyz",
            "workspace_apply_state",
        )
        self.assertIn("api POST /api/xyz/reload", function)
        self.assertIn("'{\"confirmed\":true,\"timeout\":120}'", function)
        self.assertNotIn("workspaceFingerprint", function)
        self.assertIn("validate_xyz_reload", function)

    def test_proposal_apply_timeout_is_recoverable_only_after_commit(self) -> None:
        saved_path = ROOT / "docker" / "demo-sources" / "workspace-demo.json"
        saved = json.loads(saved_path.read_text(encoding="utf-8"))
        fingerprint = "e" * 64
        response = {
            "proposal": {
                "status": "applied",
                "candidate": saved,
                "appliedFingerprint": fingerprint,
            },
            "reload": {
                "expectedWorkspaceFingerprint": fingerprint,
                "status": {
                    "completed": True,
                    "healthy": True,
                    "workspaceFingerprint": fingerprint,
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            written = 0

            def response_file(payload: dict) -> str:
                nonlocal written
                written += 1
                path = Path(directory) / f"apply-{written}.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                return str(path)

            ready = run_embedded_program(
                "workspace_apply_state",
                str(saved_path),
                "200",
                response_file(response),
            )
            self.assertEqual(0, ready.returncode, ready.stderr)
            self.assertEqual("ready", ready.stdout.strip())

            timed_out = run_embedded_program(
                "workspace_apply_state",
                str(saved_path),
                "504",
                response_file({
                    **response,
                    "reload": {
                        **response["reload"],
                        "status": {
                            **response["reload"]["status"],
                            "completed": False,
                        },
                    },
                }),
            )
            self.assertEqual(0, timed_out.returncode, timed_out.stderr)
            self.assertEqual("recover", timed_out.stdout.strip())

            uncommitted = run_embedded_program(
                "workspace_apply_state",
                str(saved_path),
                "504",
                response_file({
                    **response,
                    "proposal": {**response["proposal"], "status": "applying"},
                }),
            )
            self.assertNotEqual(0, uncommitted.returncode)
            self.assertIn("did not commit", uncommitted.stderr)

        apply_start = self.source.index("apply_saved_workspace_proposal()")
        apply_end = self.source.index(
            '\nstep "Reconciling the saved workspace\'s derived layers"',
            apply_start,
        )
        apply_function = self.source[apply_start:apply_end]
        self.assertIn("--write-out", apply_function)
        self.assertIn("workspace_apply_state", apply_function)
        self.assertIn("ensure_saved_workspace_xyz", apply_function)

    def test_apply_response_never_passes_through_argv(self) -> None:
        """A response holding several workspace copies exceeds MAX_ARG_STRLEN.

        Passing it as an argument made ./bin/mapp demo fail the publishing
        step with "Argument list too long", so the body must reach python
        through a file that curl writes.
        """
        saved_path = ROOT / "docker" / "demo-sources" / "workspace-demo.json"
        saved = json.loads(saved_path.read_text(encoding="utf-8"))
        fingerprint = "e" * 64
        proposal = {
            "status": "applied",
            "candidate": saved,
            "appliedFingerprint": fingerprint,
            "original": saved,
            "operations": [
                {"op": "set", "path": f"/{key}", "value": value}
                for key, value in saved.items()
            ],
            "diff": [
                {"op": "replace", "path": f"/{key}", "old": value,
                 "value": value}
                for key, value in saved.items()
            ],
        }
        reload_evidence = {
            "expectedWorkspaceFingerprint": fingerprint,
            "status": {
                "completed": True,
                "healthy": True,
                "workspaceFingerprint": fingerprint,
            },
        }
        body = json.dumps({
            "proposal": proposal,
            "reload": reload_evidence,
            "operation": {
                "id": "operation",
                "status": "succeeded",
                "result": {"proposal": proposal, "reload": reload_evidence},
            },
        }, separators=(",", ":"))
        self.assertGreater(len(body.encode()), 131072, "MAX_ARG_STRLEN")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "apply.json"
            path.write_text(body, encoding="utf-8")
            ready = run_embedded_program(
                "workspace_apply_state",
                str(saved_path),
                "200",
                str(path),
            )
        self.assertEqual(0, ready.returncode, ready.stderr)
        self.assertEqual("ready", ready.stdout.strip())

        state_function = self.function_source(
            "workspace_apply_state",
            "apply_saved_workspace_proposal",
        )
        self.assertIn(
            'response = json.load(open(sys.argv[3], encoding="utf-8"))',
            state_function,
        )
        apply_start = self.source.index("apply_saved_workspace_proposal()")
        apply_end = self.source.index(
            '\nstep "Reconciling the saved workspace\'s derived layers"',
            apply_start,
        )
        apply_function = self.source[apply_start:apply_end]
        self.assertIn("--output", apply_function)
        self.assertIn('"${http_status}" "${payload_file}"', apply_function)


if __name__ == "__main__":
    unittest.main()
