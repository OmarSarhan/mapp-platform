from __future__ import annotations

import csv
import hashlib
import io
import re
import stat
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from http.client import HTTPException
from pathlib import PurePosixPath
from typing import IO, Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .census_config import CensusConfig, CensusTopic


_CSV_PREFIX = ("date", "geography", "geography code")
_TS007A_LABELS = (
    "Age: Total",
    "Age: Aged 4 years and under",
    "Age: Aged 5 to 9 years",
    "Age: Aged 10 to 14 years",
    "Age: Aged 15 to 19 years",
    "Age: Aged 20 to 24 years",
    "Age: Aged 25 to 29 years",
    "Age: Aged 30 to 34 years",
    "Age: Aged 35 to 39 years",
    "Age: Aged 40 to 44 years",
    "Age: Aged 45 to 49 years",
    "Age: Aged 50 to 54 years",
    "Age: Aged 55 to 59 years",
    "Age: Aged 60 to 64 years",
    "Age: Aged 65 to 69 years",
    "Age: Aged 70 to 74 years",
    "Age: Aged 75 to 79 years",
    "Age: Aged 80 to 84 years",
    "Age: Aged 85 years and over",
)
_GEOGRAPHY_CODE_RE = re.compile(r"^[EW]\d{8}$")
_INTEGER_RE = re.compile(r"^-?\d+$")
_DECIMAL_RE = re.compile(r"^-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_METADATA_LIMIT_BYTES = 1_048_576
_READ_CHUNK_BYTES = 65_536

NumericValue = int | Decimal


class NomisError(RuntimeError):
    pass


@dataclass(frozen=True)
class NomisRow:
    oa_code: str
    values: tuple[NumericValue, ...]


@dataclass(frozen=True)
class NomisSourceMetadata:
    source_url: str
    oa_member: str
    metadata_member: str | None
    metadata_sha256: str | None
    metadata_text: str | None
    archive_sha256: str
    archive_bytes: int
    title: str
    issued: str | None
    version: int | None


@dataclass(frozen=True)
class NomisTopicStream:
    topic: CensusTopic
    source: NomisSourceMetadata
    labels: tuple[str, ...]
    target_columns: tuple[str, ...]
    rows: Iterator[NomisRow]


@dataclass
class _DownloadedArchive:
    stream: IO[bytes]
    sha256: str
    byte_count: int

    def close(self) -> None:
        self.stream.close()


class NomisClient:
    def __init__(
        self,
        config: CensusConfig,
        *,
        opener: Callable[..., Any] = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._opener = opener
        self._sleeper = sleeper

    def _download(self, topic: CensusTopic) -> _DownloadedArchive:
        source_url = topic.source_url
        parsed_url = urlparse(source_url)
        if parsed_url.scheme != "https" or parsed_url.hostname != "www.nomisweb.co.uk":
            raise NomisError(f"unsupported Nomis source URL for {topic.id}")

        last_error: Exception | None = None
        for attempt in range(self.config.http_retries + 1):
            archive = tempfile.SpooledTemporaryFile(
                max_size=self.config.spool_memory_bytes,
                mode="w+b",
            )
            try:
                request = Request(
                    source_url,
                    headers={
                        "Accept": "application/zip",
                        "User-Agent": "mapp-explore-census-etl/0.1",
                    },
                )
                with self._opener(
                    request, timeout=self.config.http_timeout_seconds
                ) as response:
                    final_url_getter = getattr(response, "geturl", None)
                    final_url = (
                        final_url_getter() if callable(final_url_getter) else source_url
                    )
                    parsed_final_url = urlparse(final_url)
                    if (
                        parsed_final_url.scheme != "https"
                        or parsed_final_url.hostname != "www.nomisweb.co.uk"
                    ):
                        raise NomisError(
                            f"Nomis redirected {topic.id} to an unsupported URL"
                        )
                    self._validate_content_length(response, topic)

                    digest = hashlib.sha256()
                    byte_count = 0
                    while True:
                        chunk = response.read(_READ_CHUNK_BYTES)
                        if not chunk:
                            break
                        if not isinstance(chunk, bytes):
                            raise NomisError(
                                f"Nomis returned non-binary content for {topic.id}"
                            )
                        byte_count += len(chunk)
                        if byte_count > self.config.max_archive_bytes:
                            raise NomisError(
                                f"Nomis archive for {topic.id} exceeds "
                                "max_archive_bytes"
                            )
                        archive.write(chunk)
                        digest.update(chunk)

                if byte_count == 0:
                    raise NomisError(f"Nomis returned an empty archive for {topic.id}")
                archive_sha256 = digest.hexdigest()
                if byte_count != topic.archive_bytes:
                    raise NomisError(
                        f"Nomis archive size mismatch for {topic.id}: "
                        f"expected {topic.archive_bytes}, received {byte_count}"
                    )
                if archive_sha256 != topic.sha256:
                    raise NomisError(f"Nomis archive SHA-256 mismatch for {topic.id}")
                archive.seek(0)
                return _DownloadedArchive(
                    stream=archive,
                    sha256=archive_sha256,
                    byte_count=byte_count,
                )
            except NomisError:
                archive.close()
                raise
            except (HTTPError, URLError, TimeoutError, OSError, HTTPException) as exc:
                archive.close()
                last_error = exc
                if attempt >= self.config.http_retries:
                    break
                self._sleeper(min(8.0, 0.5 * (2**attempt)))

        raise NomisError(
            f"Nomis download failed for {topic.id} after "
            f"{self.config.http_retries + 1} attempts: {last_error}"
        ) from last_error

    def _validate_content_length(self, response: Any, topic: CensusTopic) -> None:
        headers = getattr(response, "headers", None)
        raw_length = headers.get("Content-Length") if headers is not None else None
        if raw_length is None:
            return
        try:
            content_length = int(raw_length)
        except (TypeError, ValueError) as exc:
            raise NomisError(
                f"Nomis returned an invalid Content-Length for {topic.id}"
            ) from exc
        if content_length < 0:
            raise NomisError(f"Nomis returned an invalid Content-Length for {topic.id}")
        if content_length > self.config.max_archive_bytes:
            raise NomisError(f"Nomis archive for {topic.id} exceeds max_archive_bytes")
        if content_length != topic.archive_bytes:
            raise NomisError(
                f"Nomis Content-Length mismatch for {topic.id}: "
                f"expected {topic.archive_bytes}, received {content_length}"
            )

    @contextmanager
    def open_topic(self, topic: CensusTopic) -> Iterator[NomisTopicStream]:
        if topic not in self.config.topics:
            raise NomisError(f"topic {topic.id} is not present in the loaded manifest")
        archive = self._download(topic)
        try:
            try:
                zipped = zipfile.ZipFile(archive.stream)
            except (OSError, zipfile.BadZipFile) as exc:
                raise NomisError(f"malformed Nomis ZIP for {topic.id}") from exc

            with zipped:
                oa_info, metadata_info = self._validate_members(zipped, topic)
                source_metadata = self._read_metadata(
                    zipped,
                    metadata_info,
                    topic,
                    archive,
                )
                try:
                    raw_csv = zipped.open(oa_info)
                except (KeyError, RuntimeError, zipfile.BadZipFile) as exc:
                    raise NomisError(
                        f"cannot open OA CSV member for {topic.id}"
                    ) from exc
                with raw_csv:
                    with io.TextIOWrapper(
                        raw_csv, encoding="utf-8-sig", newline=""
                    ) as text_csv:
                        reader = csv.reader(text_csv, strict=True)
                        labels = self._read_header(reader, topic)
                        rows = self._iter_rows(reader, topic)
                        yield NomisTopicStream(
                            topic=topic,
                            source=source_metadata,
                            labels=labels,
                            target_columns=topic.target_columns,
                            rows=rows,
                        )
        finally:
            archive.close()

    def _validate_members(
        self,
        archive: zipfile.ZipFile,
        topic: CensusTopic,
    ) -> tuple[zipfile.ZipInfo, zipfile.ZipInfo | None]:
        members = archive.infolist()
        if not members:
            raise NomisError(f"empty Nomis ZIP for {topic.id}")

        names: set[str] = set()
        for member in members:
            self._validate_member(member, topic)
            if member.filename in names:
                raise NomisError(
                    f"duplicate ZIP member {member.filename!r} for {topic.id}"
                )
            names.add(member.filename)

        try:
            oa_info = archive.getinfo(topic.oa_member)
        except KeyError as exc:
            raise NomisError(
                f"missing OA CSV member {topic.oa_member!r} for {topic.id}"
            ) from exc
        if oa_info.is_dir() or oa_info.file_size == 0:
            raise NomisError(f"empty OA CSV member for {topic.id}")

        metadata_pattern = re.compile(
            rf"^metadata/{re.escape(topic.slug)}-2021-(\d+)\.txt$"
        )
        all_metadata_members = [
            member
            for member in members
            if PurePosixPath(member.filename).parts[0] == "metadata"
        ]
        metadata_members = [
            member
            for member in all_metadata_members
            if metadata_pattern.fullmatch(member.filename)
        ]
        if len(metadata_members) != len(all_metadata_members):
            raise NomisError(f"unexpected metadata member for {topic.id}")
        if topic.id == "TS007A" and metadata_members:
            raise NomisError("TS007A must not contain inferred metadata")
        if topic.id == "TS007A":
            return oa_info, None
        if len(metadata_members) != 1:
            raise NomisError(
                f"expected exactly one metadata member for {topic.id}, "
                f"found {len(metadata_members)}"
            )
        metadata_info = metadata_members[0]
        if metadata_info.is_dir() or metadata_info.file_size == 0:
            raise NomisError(f"empty metadata member for {topic.id}")
        if metadata_info.file_size > _METADATA_LIMIT_BYTES:
            raise NomisError(f"metadata member is too large for {topic.id}")
        return oa_info, metadata_info

    def _validate_member(self, member: zipfile.ZipInfo, topic: CensusTopic) -> None:
        name = member.filename
        path = PurePosixPath(name)
        if (
            not name
            or "\x00" in name
            or "\\" in name
            or path.is_absolute()
            or (path.parts and ":" in path.parts[0])
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise NomisError(f"unsafe ZIP member {name!r} for {topic.id}")
        if member.flag_bits & 0x1:
            raise NomisError(f"encrypted ZIP member {name!r} for {topic.id}")
        unix_mode = member.external_attr >> 16
        if unix_mode and stat.S_ISLNK(unix_mode):
            raise NomisError(f"symlink ZIP member {name!r} for {topic.id}")
        if member.file_size > self.config.max_archive_bytes:
            raise NomisError(f"ZIP member {name!r} is too large for {topic.id}")

    def _read_metadata(
        self,
        archive: zipfile.ZipFile,
        metadata_info: zipfile.ZipInfo | None,
        topic: CensusTopic,
        downloaded: _DownloadedArchive,
    ) -> NomisSourceMetadata:
        if metadata_info is None:
            return NomisSourceMetadata(
                source_url=topic.source_url,
                oa_member=topic.oa_member,
                metadata_member=None,
                metadata_sha256=None,
                metadata_text=None,
                archive_sha256=downloaded.sha256,
                archive_bytes=downloaded.byte_count,
                title=topic.title,
                issued=None,
                version=None,
            )
        try:
            with archive.open(metadata_info) as source:
                raw_metadata = source.read(_METADATA_LIMIT_BYTES + 1)
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise NomisError(f"cannot read metadata for {topic.id}") from exc
        if len(raw_metadata) > _METADATA_LIMIT_BYTES:
            raise NomisError(f"metadata member is too large for {topic.id}")
        try:
            text = raw_metadata.decode("utf-8-sig")
        except UnicodeError as exc:
            raise NomisError(f"metadata is not UTF-8 for {topic.id}") from exc

        fields: dict[str, str] = {}
        required = {"Title", "Issued", "Version"}
        for line in text.splitlines():
            if line.strip() == "Area Type":
                break
            key, separator, value = line.partition(":")
            if not separator or key not in required:
                continue
            if key in fields:
                raise NomisError(f"duplicate metadata field {key!r} for {topic.id}")
            fields[key] = value.strip()
        missing = required.difference(fields)
        if missing or any(not fields[key] for key in required):
            raise NomisError(
                f"metadata is missing required fields for {topic.id}: "
                f"{', '.join(sorted(missing)) if missing else 'empty value'}"
            )
        if not re.fullmatch(r"\d+", fields["Version"]):
            raise NomisError(f"invalid metadata version for {topic.id}")
        version = int(fields["Version"])
        if version <= 0:
            raise NomisError(f"invalid metadata version for {topic.id}")
        member_version = re.search(r"-(\d+)\.txt$", metadata_info.filename)
        if member_version is None or int(member_version.group(1)) != version:
            raise NomisError(f"metadata filename/version mismatch for {topic.id}")

        return NomisSourceMetadata(
            source_url=topic.source_url,
            oa_member=topic.oa_member,
            metadata_member=metadata_info.filename,
            metadata_sha256=hashlib.sha256(raw_metadata).hexdigest(),
            metadata_text=text,
            archive_sha256=downloaded.sha256,
            archive_bytes=downloaded.byte_count,
            title=fields["Title"],
            issued=fields["Issued"],
            version=version,
        )

    def _read_header(
        self,
        reader: Iterator[list[str]],
        topic: CensusTopic,
    ) -> tuple[str, ...]:
        try:
            header = next(reader)
        except StopIteration as exc:
            raise NomisError(f"empty OA CSV for {topic.id}") from exc
        except (csv.Error, UnicodeError, OSError, zipfile.BadZipFile) as exc:
            raise NomisError(f"malformed OA CSV header for {topic.id}") from exc

        expected_width = len(_CSV_PREFIX) + topic.value_column_count
        if tuple(header[: len(_CSV_PREFIX)]) != _CSV_PREFIX:
            raise NomisError(f"unsupported OA CSV header prefix for {topic.id}")
        if len(header) != expected_width:
            raise NomisError(
                f"OA CSV value-column mismatch for {topic.id}: "
                f"expected {topic.value_column_count}, "
                f"received {max(0, len(header) - len(_CSV_PREFIX))}"
            )
        if any(not label.strip() for label in header):
            raise NomisError(f"OA CSV has an empty header for {topic.id}")
        if len(header) != len(set(header)):
            raise NomisError(f"OA CSV has duplicate headers for {topic.id}")

        labels = tuple(header[len(_CSV_PREFIX) :])
        if topic.id == "TS007A":
            if labels != _TS007A_LABELS:
                raise NomisError("OA CSV has unsupported TS007A value headers")
        else:
            nomis_measure_style = all(
                label.endswith("; measures: Value") for label in labels
            )
            ons_plain_style = all(
                ": " in label and not label.endswith("; measures: Value")
                for label in labels
            )
            if not nomis_measure_style and not ons_plain_style:
                raise NomisError(f"OA CSV has unsupported value headers for {topic.id}")
        return labels

    def _iter_rows(
        self,
        reader: Iterator[list[str]],
        topic: CensusTopic,
    ) -> Iterator[NomisRow]:
        seen_codes: set[str] = set()
        england_count = 0
        expected_width = len(_CSV_PREFIX) + topic.value_column_count
        try:
            for row_number, row in enumerate(reader, start=2):
                if len(row) != expected_width:
                    raise NomisError(
                        f"OA CSV row {row_number} has {len(row)} columns for "
                        f"{topic.id}; expected {expected_width}"
                    )
                date, geography, geography_code = row[:3]
                if date != "2021":
                    raise NomisError(
                        f"OA CSV row {row_number} has unsupported date for {topic.id}"
                    )
                if (
                    not _GEOGRAPHY_CODE_RE.fullmatch(geography_code)
                    or geography != geography_code
                ):
                    raise NomisError(
                        f"OA CSV row {row_number} has invalid geography for "
                        f"{topic.id}"
                    )
                if geography_code in seen_codes:
                    raise NomisError(
                        f"duplicate OA code {geography_code} for {topic.id}"
                    )
                seen_codes.add(geography_code)
                values = tuple(
                    self._numeric_value(value, topic, row_number)
                    for value in row[len(_CSV_PREFIX) :]
                )
                if geography_code.startswith("E"):
                    england_count += 1
                    yield NomisRow(oa_code=geography_code, values=values)
        except NomisError:
            raise
        except (csv.Error, UnicodeError, OSError, zipfile.BadZipFile) as exc:
            raise NomisError(f"malformed OA CSV data for {topic.id}") from exc

        if england_count != self.config.expected_england_oa_count:
            raise NomisError(
                f"England OA row-count mismatch for {topic.id}: expected "
                f"{self.config.expected_england_oa_count}, received {england_count}"
            )

    def _numeric_value(
        self,
        raw: str,
        topic: CensusTopic,
        row_number: int,
    ) -> NumericValue:
        if not raw or raw != raw.strip():
            raise NomisError(
                f"invalid numeric value at row {row_number} for {topic.id}"
            )
        if _INTEGER_RE.fullmatch(raw):
            try:
                value: NumericValue = int(raw)
            except ValueError as exc:
                raise NomisError(
                    f"invalid numeric value at row {row_number} for {topic.id}"
                ) from exc
        else:
            try:
                decimal_value = Decimal(raw)
            except InvalidOperation as exc:
                raise NomisError(
                    f"invalid numeric value at row {row_number} for {topic.id}"
                ) from exc
            if not decimal_value.is_finite():
                raise NomisError(
                    f"non-finite numeric value at row {row_number} for {topic.id}"
                )
            if not _DECIMAL_RE.fullmatch(raw):
                raise NomisError(
                    f"invalid numeric value at row {row_number} for {topic.id}"
                )
            value = decimal_value
        if value < 0:
            raise NomisError(
                f"negative numeric value at row {row_number} for {topic.id}"
            )
        return value
