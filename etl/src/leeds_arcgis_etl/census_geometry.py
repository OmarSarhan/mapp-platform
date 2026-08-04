from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .config import LayerConfig
from .core import PreparedFeature


OA_CODE_RE = re.compile(r"^E[0-9]{8}$")


class CensusGeometryError(RuntimeError):
    pass


class CensusGeometryAudit:
    """Validate and hash one deterministic, OID-ordered ArcGIS feature scan."""

    def __init__(self, layer: LayerConfig, metadata: dict[str, Any]) -> None:
        oa_index = next(
            (
                index
                for index, column in enumerate(layer.columns)
                if column.target == "oa21cd"
            ),
            None,
        )
        if oa_index is None:
            raise CensusGeometryError("Census geometry layer does not map oa21cd")
        self._oa_index = oa_index
        self._hash = hashlib.sha256()
        try:
            canonical_metadata = json.dumps(
                metadata,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise CensusGeometryError(
                f"Census geometry metadata is not canonical JSON: {exc}"
            ) from exc
        self._hash.update(canonical_metadata)
        self.codes: set[str] = set()
        self.row_count = 0

    def add(self, feature: PreparedFeature, *, offset: int) -> None:
        code = feature.values[self._oa_index]
        if not isinstance(code, str) or not OA_CODE_RE.fullmatch(code):
            raise CensusGeometryError(
                "Census geometry contains an invalid England OA code "
                f"at ArcGIS offset {offset}: {code!r}"
            )
        if feature.geometry is None:
            raise CensusGeometryError(f"Census geometry is null for {code}")
        if code in self.codes:
            raise CensusGeometryError(
                f"Census geometry contains duplicate OA code {code}"
            )
        self.codes.add(code)
        self.row_count += 1
        self._hash.update(bytes.fromhex(feature.source_hash))

    @property
    def sha256(self) -> str:
        return self._hash.hexdigest()
