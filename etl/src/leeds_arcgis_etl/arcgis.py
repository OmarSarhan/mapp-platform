from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Iterator
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import LayerConfig


LOGGER = logging.getLogger(__name__)


class ArcGISError(RuntimeError):
    pass


def _format_service_error(url: str, error: Any) -> str:
    code: int | str | None = None
    descriptions: list[str] = []
    if isinstance(error, dict):
        raw_code = error.get("code")
        if (
            isinstance(raw_code, (int, str))
            and not isinstance(raw_code, bool)
            and str(raw_code).strip()
        ):
            code = raw_code

        message = error.get("message")
        if isinstance(message, str) and message.strip():
            descriptions.append(message.strip())

        details = error.get("details")
        if isinstance(details, list):
            useful_details = [
                detail.strip()
                for detail in details
                if isinstance(detail, str) and detail.strip()
            ]
            if useful_details:
                descriptions.append(f"Details: {'; '.join(useful_details)}")
    elif isinstance(error, str) and error.strip():
        descriptions.append(error.strip())

    code_suffix = f" (code {code})" if code is not None else ""
    description = (
        " ".join(descriptions)
        if descriptions
        else "The service did not provide an error message."
    )
    return f"ArcGIS service error from {url}{code_suffix}: {description}"


class ArcGISClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 30,
        retries: int = 4,
        page_size: int = 500,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.page_size = page_size

    def _request(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        request_url = f"{url}?{urlencode(params)}"
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            request = Request(
                request_url,
                headers={
                    "Accept": "application/json, application/geo+json",
                    "User-Agent": "mapp-explore-leeds-etl/0.1",
                },
            )
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.load(response)
                if not isinstance(payload, dict):
                    raise ArcGISError(
                        f"ArcGIS returned a non-object response from {url}"
                    )
                if "error" in payload:
                    error = payload["error"]
                    raise ArcGISError(_format_service_error(url, error))
                return payload
            except ArcGISError:
                raise
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                delay = min(8.0, 0.5 * (2**attempt)) + random.uniform(0, 0.25)
                LOGGER.warning(
                    "ArcGIS request failed (attempt %s/%s); retrying in %.2fs: %s",
                    attempt + 1,
                    self.retries + 1,
                    delay,
                    exc,
                )
                time.sleep(delay)
        raise ArcGISError(
            f"ArcGIS request failed after {self.retries + 1} attempts: {last_error}"
        ) from last_error

    def metadata(self, layer: LayerConfig) -> dict[str, Any]:
        return self._request(layer.source_url, {"f": "pjson"})

    def count(self, layer: LayerConfig) -> int:
        payload = self._request(
            f"{layer.source_url}/query",
            {
                "f": "json",
                "where": layer.where,
                "returnCountOnly": "true",
            },
        )
        count = payload.get("count")
        if not isinstance(count, int) or count < 0:
            raise ArcGISError(f"invalid count response for {layer.key}: {payload}")
        return count

    def pages(
        self,
        layer: LayerConfig,
        metadata: dict[str, Any],
    ) -> Iterator[tuple[int, list[dict[str, Any]]]]:
        server_limit = metadata.get("maxRecordCount")
        if not isinstance(server_limit, int) or server_limit < 1:
            raise ArcGISError(f"layer {layer.key} has no usable maxRecordCount")
        page_size = min(self.page_size, server_limit)
        offset = 0

        while True:
            payload = self._request(
                f"{layer.source_url}/query",
                {
                    "f": "geojson",
                    "where": layer.where,
                    "outFields": ",".join(layer.out_fields),
                    "returnGeometry": "true",
                    "returnZ": "false",
                    "returnM": "false",
                    "outSR": "4326",
                    "orderByFields": f"{layer.object_id_field} ASC",
                    "resultOffset": offset,
                    "resultRecordCount": page_size,
                },
            )
            if payload.get("type") != "FeatureCollection":
                raise ArcGISError(
                    f"invalid GeoJSON response for {layer.key} at offset {offset}"
                )
            features = payload.get("features")
            if not isinstance(features, list):
                raise ArcGISError(
                    f"GeoJSON response for {layer.key} has no features array"
                )
            if not features:
                return
            for feature in features:
                if not isinstance(feature, dict):
                    raise ArcGISError(
                        f"GeoJSON response for {layer.key} contains a non-object feature"
                    )
            yield offset, features
            offset += len(features)

            exceeded = payload.get("exceededTransferLimit")
            if exceeded is False:
                return
            if exceeded is None and len(features) < page_size:
                return
