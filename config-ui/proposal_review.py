"""Retained visual review bundles and exact proposal/evidence bindings."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from visual_artifacts import read_visual_image, issue_download, VisualArtifactError

PREVIEW_MAX_AGE_SECONDS = 24 * 60 * 60
BINDING_FIELDS = ("proposalId", "candidateHash", "originalRevision", "evidenceOperationId", "evidenceFingerprint")


class ProposalReviewError(ValueError):
    def __init__(self, message, code="proposal.preview_required"):
        super().__init__(message)
        self.code = code


def review_bundle(operation: dict, root: Path, key: bytes, origin: str, *, now=None) -> dict:
    """At most six original PNGs, no inline bytes or stored signed URLs."""
    result = operation.get("result") or {}
    target = operation.get("target") or {}
    visual = result.get("visual") or {}
    plan = result.get("plan") or {}
    artifacts = visual.get("artifacts") or {}
    requirements = plan.get("reviewRequirements") or {"legend": True, "popup": True}
    captures = []
    for side, prefix in (("original", "before"), ("candidate", "after")):
        side_requirements = requirements.get(side, requirements)
        for kind, suffix in (("map", "Map"), ("legend", "StylingPanel"), ("popup", "InfoPanel")):
            path = artifacts.get(prefix + suffix)
            if side == "candidate" and not path:
                path = artifacts.get({"map": "map", "legend": "stylingPanel", "popup": "infoPanel"}[kind])
            item = {"side": side, "kind": kind, "status": "missing"}
            if path:
                try:
                    image = read_visual_image(root, path, include_data=False)
                    download = issue_download(image, key)
                    if origin:
                        download["url"] = origin + download["path"]
                    item.update(status="captured", **image, download=download)
                except VisualArtifactError as exc:
                    item.update(status="unavailable", reason=exc.code)
            elif kind != "map" and (side_requirements.get(kind) is False or (plan.get("evidenceApplicability") or {}).get(side) is False):
                item["status"] = "not-applicable"
            captures.append(item)
    missing = [item["kind"] for item in captures if item["side"] == "candidate"
               and item["status"] not in {"captured", "not-applicable"}]
    gaps = [{"side": item["side"], "kind": item["kind"], "status": item["status"]}
            for item in captures if item["status"] not in {"captured", "not-applicable"}]
    binding = {
        "proposalId": result.get("proposalId") or target.get("proposalId"),
        "candidateHash": result.get("candidateHash") or target.get("candidateHash"),
        "originalRevision": result.get("originalRevision") or target.get("originalRevision"),
        "evidenceOperationId": operation.get("id"),
    }
    # Link expiry and signatures are deliberately excluded: renewal must not
    # change what was reviewed. Replaced/deleted PNG bytes must change it.
    stable = {"binding": binding, "created": operation.get("created"),
              "renderPassed": visual.get("renderPassed"), "passed": visual.get("passed"),
              "captures": [{name: item.get(name) for name in ("side", "kind", "status", "path", "sha256")}
                           for item in captures]}
    binding["evidenceFingerprint"] = hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    current = datetime.now(timezone.utc).timestamp() if now is None else now
    try:
        created = datetime.fromisoformat(operation["created"].replace("Z", "+00:00")).timestamp()
        fresh = 0 <= current - created <= PREVIEW_MAX_AGE_SECONDS
    except (KeyError, TypeError, ValueError):
        fresh = False
    eligible = (operation.get("kind") in {"proposal.screenshot", "proposal.visual-test"}
                and operation.get("status") in {"succeeded", "failed"}
                and visual.get("renderPassed") is True and "map" not in missing
                and all(binding.values()) and fresh)
    return {"binding": binding, "captures": captures, "missingCandidateCaptures": missing,
            "renderPassed": visual.get("renderPassed") is True,
            "checksPassed": visual.get("passed") is True,
            "complete": not gaps and visual.get("passed") is True,
            "missingCaptures": gaps,
            "eligible": bool(eligible), "fresh": fresh,
            "previewCreated": operation.get("created"), "maximumAgeSeconds": PREVIEW_MAX_AGE_SECONDS,
            "boundary": plan.get("boundary"),
            "renewal": "Poll visual_operations_show again to renew the one-hour download links."}


def validate_review(proposal: dict, payload: dict, operation: dict, bundle: dict) -> dict:
    binding = bundle["binding"]
    expected = {"proposalId": proposal["id"], "candidateHash": proposal["candidateHash"],
                "originalRevision": proposal["originalRevision"]}
    if any(binding.get(name) != value for name, value in expected.items()):
        raise ProposalReviewError("Preview does not match this proposal and revision. Render a new preview.", "proposal.preview_mismatch")
    if any(payload.get(name) != binding.get(name) for name in BINDING_FIELDS if name != "proposalId"):
        raise ProposalReviewError("Confirmation does not match the retained preview. Review it and confirm again.", "proposal.preview_mismatch")
    if not bundle["eligible"]:
        raise ProposalReviewError("A fresh retained candidate map preview is required before approval. Render a new preview.", "proposal.preview_stale")
    if not bundle["complete"] and payload.get("acknowledgeIncompletePreview") is not True:
        raise ProposalReviewError("Preview captures or checks are incomplete. Render complete evidence, or explicitly acknowledge the listed gaps when confirming.", "proposal.preview_incomplete")
    return dict(binding)
