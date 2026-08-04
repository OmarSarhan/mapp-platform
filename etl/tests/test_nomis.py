from __future__ import annotations

import csv
import hashlib
import io
import unittest
import warnings
import zipfile
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.error import URLError

from leeds_arcgis_etl.census_config import CensusTopic, load_census_config
from leeds_arcgis_etl.nomis import NomisClient, NomisError


ROOT = Path(__file__).resolve().parents[2]
BASE_CONFIG = load_census_config(ROOT / "etl" / "config" / "census.json")
VALUE_LABELS = (
    "Measure: Total; measures: Value",
    "Measure: Integer; measures: Value",
    "Measure: Decimal; measures: Value",
)
TS007A_LABELS = (
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


class FakeResponse(io.BytesIO):
    def __init__(
        self,
        payload: bytes,
        *,
        final_url: str,
        content_length: str | None = None,
    ) -> None:
        super().__init__(payload)
        self._final_url = final_url
        self.headers = (
            {} if content_length is None else {"Content-Length": content_length}
        )

    def geturl(self) -> str:
        return self._final_url


class FakeOpener:
    def __init__(
        self,
        payloads: list[bytes | Exception],
        *,
        final_url: str | None = None,
        content_length: str | None = None,
    ) -> None:
        self.payloads = payloads
        self.final_url = final_url
        self.content_length = content_length
        self.calls: list[tuple[Any, float]] = []

    def __call__(self, request: Any, *, timeout: float) -> FakeResponse:
        self.calls.append((request, timeout))
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return FakeResponse(
            payload,
            final_url=self.final_url or request.full_url,
            content_length=self.content_length,
        )


def csv_bytes(
    rows: list[list[str]],
    *,
    labels: tuple[str, ...] = VALUE_LABELS,
    prefix: tuple[str, ...] = ("date", "geography", "geography code"),
) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow((*prefix, *labels))
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def metadata_bytes(title: str, *, version: int = 1) -> bytes:
    return (
        f"Title: {title}\n"
        "Description: Census estimates with reviewed topic definitions.\n"
        "Issued: 2022-12-13T00:00:00.000Z\n"
        "Unit of measure: Person\n"
        f"Version: {version}\n"
        "\nArea Type\n"
        "Output areas are the lowest published Census geography.\n"
        "\nCoverage\n"
        "Census 2021 statistics cover England and Wales.\n"
        "\nProtecting personal data\n"
        "Counts use record swapping and cell key perturbation.\n"
        "\nDimensions:\n"
        "\tID: residence_type\n"
        "\tDescription: Whether a person lives in a household.\n"
        "\tQuality Statement: https://example.test/quality\n"
    ).encode("utf-8")


def make_archive(
    *,
    topic_id: str = "TS001",
    title: str = (
        "Number of usual residents in households and communal establishments"
    ),
    csv_payload: bytes | None = None,
    include_oa: bool = True,
    include_metadata: bool = True,
    metadata_name: str | None = None,
    metadata_payload: bytes | None = None,
    extra_members: tuple[tuple[str, bytes], ...] = (),
    compression: int = zipfile.ZIP_STORED,
) -> bytes:
    slug = topic_id.lower()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        if include_oa:
            archive.writestr(
                f"census2021-{slug}-oa.csv",
                (
                    csv_payload
                    if csv_payload is not None
                    else csv_bytes(
                        [
                            ["2021", "E00000001", "E00000001", "1", "2", "3.5"],
                            ["2021", "W00000001", "W00000001", "4", "5", "6"],
                            ["2021", "E00000002", "E00000002", "7", "8", "9"],
                        ]
                    )
                ),
            )
        if include_metadata:
            archive.writestr(
                metadata_name or f"metadata/{slug}-2021-1.txt",
                (
                    metadata_payload
                    if metadata_payload is not None
                    else metadata_bytes(title)
                ),
            )
        for name, payload in extra_members:
            archive.writestr(name, payload)
    return output.getvalue()


def pinned_topic(
    payload: bytes,
    *,
    topic_id: str = "TS001",
    title: str = (
        "Number of usual residents in households and communal establishments"
    ),
    value_column_count: int = 3,
) -> CensusTopic:
    return CensusTopic(
        id=topic_id,
        title=title,
        value_column_count=value_column_count,
        archive_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def client_for(
    payloads: list[bytes | Exception],
    *,
    topic: CensusTopic,
    expected_count: int = 2,
    retries: int = 0,
    max_archive_bytes: int = 1_048_576,
    final_url: str | None = None,
    content_length: str | None = None,
) -> tuple[NomisClient, FakeOpener, list[float]]:
    config = replace(
        BASE_CONFIG,
        topics=(topic,),
        expected_england_oa_count=expected_count,
        http_retries=retries,
        max_archive_bytes=max_archive_bytes,
        spool_memory_bytes=min(256, max_archive_bytes),
    )
    opener = FakeOpener(
        payloads,
        final_url=final_url,
        content_length=content_length,
    )
    sleeps: list[float] = []
    return (
        NomisClient(config, opener=opener, sleeper=sleeps.append),
        opener,
        sleeps,
    )


class NomisSuccessTests(unittest.TestCase):
    def test_stream_filters_wales_and_preserves_labels_and_numeric_types(self) -> None:
        payload = make_archive()
        topic = pinned_topic(payload)
        client, opener, _ = client_for([payload], topic=topic)

        with client.open_topic(topic) as stream:
            rows = list(stream.rows)
            self.assertEqual(stream.labels, VALUE_LABELS)
            self.assertEqual(
                stream.target_columns,
                ("ts001_0001", "ts001_0002", "ts001_0003"),
            )
            self.assertEqual(stream.source.archive_sha256, topic.sha256)
            self.assertEqual(stream.source.archive_bytes, len(payload))
            self.assertEqual(stream.source.version, 1)
            self.assertIsNotNone(stream.source.metadata_text)
            self.assertIsNotNone(stream.source.metadata_sha256)

        self.assertEqual([row.oa_code for row in rows], ["E00000001", "E00000002"])
        self.assertEqual(rows[0].values, (1, 2, Decimal("3.5")))
        request, timeout = opener.calls[0]
        self.assertEqual(timeout, BASE_CONFIG.http_timeout_seconds)
        self.assertEqual(request.get_header("Accept"), "application/zip")

    def test_ts007a_has_explicitly_null_metadata_without_inference(self) -> None:
        rows = [
            ["2021", code, code, *(["1"] * len(TS007A_LABELS))]
            for code in ("E00000001", "W00000001", "E00000002")
        ]
        payload = make_archive(
            topic_id="TS007A",
            title="Age by five-year age bands",
            include_metadata=False,
            csv_payload=csv_bytes(rows, labels=TS007A_LABELS),
        )
        topic = pinned_topic(
            payload,
            topic_id="TS007A",
            title="Age by five-year age bands",
            value_column_count=len(TS007A_LABELS),
        )
        client, _, _ = client_for([payload], topic=topic)

        with client.open_topic(topic) as stream:
            list(stream.rows)
            self.assertEqual(stream.source.title, topic.title)
            self.assertIsNone(stream.source.metadata_member)
            self.assertIsNone(stream.source.metadata_sha256)
            self.assertIsNone(stream.source.metadata_text)
            self.assertIsNone(stream.source.issued)
            self.assertIsNone(stream.source.version)

    def test_full_metadata_document_and_exact_raw_hash_are_preserved(self) -> None:
        raw_metadata = b"\xef\xbb\xbf" + metadata_bytes(
            "Number of usual residents in households and communal establishments"
        )
        payload = make_archive(metadata_payload=raw_metadata)
        topic = pinned_topic(payload)
        client, _, _ = client_for([payload], topic=topic)

        with client.open_topic(topic) as stream:
            list(stream.rows)
            source = stream.source

        self.assertEqual(
            source.metadata_sha256,
            hashlib.sha256(raw_metadata).hexdigest(),
        )
        self.assertEqual(
            source.metadata_text,
            raw_metadata.decode("utf-8-sig"),
        )
        for retained_section in (
            "Description:",
            "Unit of measure:",
            "Area Type",
            "Coverage",
            "Protecting personal data",
            "Dimensions:",
            "Quality Statement:",
        ):
            with self.subTest(section=retained_section):
                self.assertIn(retained_section, source.metadata_text or "")

    def test_transient_network_failure_is_retried_with_bounded_backoff(self) -> None:
        payload = make_archive()
        topic = pinned_topic(payload)
        client, opener, sleeps = client_for(
            [URLError("temporary"), payload],
            topic=topic,
            retries=1,
        )

        with client.open_topic(topic) as stream:
            list(stream.rows)

        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(sleeps, [0.5])

    def test_plain_ons_labels_and_source_metadata_are_preserved_verbatim(self) -> None:
        labels = (
            "Dimension: Total",
            "Dimension: Category ",
            "Dimension: Other",
        )
        payload = make_archive(
            title="Source metadata title",
            csv_payload=csv_bytes(
                [
                    ["2021", "E00000001", "E00000001", "1", "2", "3"],
                    ["2021", "E00000002", "E00000002", "4", "5", "6"],
                ],
                labels=labels,
            ),
        )
        topic = pinned_topic(payload)
        client, _, _ = client_for([payload], topic=topic)

        with client.open_topic(topic) as stream:
            list(stream.rows)
            self.assertEqual(stream.labels, labels)
            self.assertEqual(stream.source.title, "Source metadata title")


class NomisDownloadSafetyTests(unittest.TestCase):
    def test_empty_response_and_pinned_size_or_hash_drift_are_rejected(self) -> None:
        good = make_archive()
        good_topic = pinned_topic(good)
        cases = (
            (b"", good_topic, "empty archive"),
            (good + b"x", good_topic, "size mismatch"),
            (
                good[:-1] + bytes([good[-1] ^ 1]),
                good_topic,
                "SHA-256 mismatch",
            ),
        )
        for payload, topic, message in cases:
            with self.subTest(message=message):
                client, _, _ = client_for([payload], topic=topic)
                with self.assertRaisesRegex(NomisError, message):
                    with client.open_topic(topic):
                        pass

    def test_content_length_is_validated_before_reading(self) -> None:
        payload = make_archive()
        topic = pinned_topic(payload)
        for content_length, message in (
            ("not-a-number", "invalid Content-Length"),
            (str(len(payload) + 1), "Content-Length mismatch"),
            (str(2_000_000), "exceeds max_archive_bytes"),
        ):
            with self.subTest(content_length=content_length):
                client, _, _ = client_for(
                    [payload],
                    topic=topic,
                    content_length=content_length,
                )
                with self.assertRaisesRegex(NomisError, message):
                    with client.open_topic(topic):
                        pass

    def test_streamed_body_cannot_exceed_global_limit(self) -> None:
        payload = make_archive()
        topic = pinned_topic(payload)
        client, _, _ = client_for(
            [payload],
            topic=topic,
            max_archive_bytes=len(payload) - 1,
        )

        with self.assertRaisesRegex(NomisError, "exceeds max_archive_bytes"):
            with client.open_topic(topic):
                pass

    def test_redirect_must_remain_on_the_exact_https_nomis_host(self) -> None:
        payload = make_archive()
        topic = pinned_topic(payload)
        for final_url in (
            "http://www.nomisweb.co.uk/output.zip",
            "https://example.test/output.zip",
        ):
            with self.subTest(final_url=final_url):
                client, _, _ = client_for(
                    [payload],
                    topic=topic,
                    final_url=final_url,
                )
                with self.assertRaisesRegex(NomisError, "unsupported URL"):
                    with client.open_topic(topic):
                        pass

    def test_network_retry_exhaustion_is_concise(self) -> None:
        payload = make_archive()
        topic = pinned_topic(payload)
        client, opener, sleeps = client_for(
            [URLError("one"), URLError("two")],
            topic=topic,
            retries=1,
        )

        with self.assertRaisesRegex(NomisError, "after 2 attempts"):
            with client.open_topic(topic):
                pass
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(sleeps, [0.5])

    def test_unconfigured_topic_is_rejected_before_network_access(self) -> None:
        payload = make_archive()
        topic = pinned_topic(payload)
        other = replace(topic, id="TS002")
        client, opener, _ = client_for([payload], topic=topic)

        with self.assertRaisesRegex(NomisError, "not present in the loaded manifest"):
            with client.open_topic(other):
                pass
        self.assertEqual(opener.calls, [])


class NomisArchiveSafetyTests(unittest.TestCase):
    def _assert_archive_error(self, payload: bytes, message: str) -> None:
        topic = pinned_topic(payload)
        client, _, _ = client_for([payload], topic=topic)
        with self.assertRaisesRegex(NomisError, message):
            with client.open_topic(topic):
                pass

    def test_malformed_and_empty_zip_are_rejected(self) -> None:
        malformed = b"this is not a zip"
        self._assert_archive_error(malformed, "malformed Nomis ZIP")

        empty_output = io.BytesIO()
        with zipfile.ZipFile(empty_output, "w"):
            pass
        self._assert_archive_error(empty_output.getvalue(), "empty Nomis ZIP")

    def test_missing_or_empty_required_members_are_rejected(self) -> None:
        cases = (
            (make_archive(include_oa=False), "missing OA CSV member"),
            (make_archive(csv_payload=b""), "empty OA CSV member"),
            (make_archive(include_metadata=False), "exactly one metadata"),
            (
                make_archive(extra_members=(("metadata/ts001-2021-2.txt", b"x"),)),
                "exactly one metadata",
            ),
            (
                make_archive(
                    include_metadata=False,
                    extra_members=(("metadata/unexpected.txt", b"x"),),
                ),
                "unexpected metadata",
            ),
        )
        for payload, message in cases:
            with self.subTest(message=message):
                self._assert_archive_error(payload, message)

    def test_unsafe_and_duplicate_members_are_rejected(self) -> None:
        for name in ("../escape.txt", "/absolute.txt", "C:/drive.txt", "a\\b.txt"):
            with self.subTest(name=name):
                unsafe = make_archive(extra_members=((name, b"x"),))
                self._assert_archive_error(unsafe, "unsafe ZIP member")

        output = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(output, "w") as archive:
                archive.writestr("duplicate.txt", b"one")
                archive.writestr("duplicate.txt", b"two")
        self._assert_archive_error(output.getvalue(), "duplicate ZIP member")

    def test_ts007a_rejects_any_invented_metadata(self) -> None:
        rows = [
            ["2021", code, code, *(["1"] * len(TS007A_LABELS))]
            for code in ("E00000001", "W00000001", "E00000002")
        ]
        payload = make_archive(
            topic_id="TS007A",
            title="Age by five-year age bands",
            include_metadata=True,
            csv_payload=csv_bytes(rows, labels=TS007A_LABELS),
        )
        topic = pinned_topic(
            payload,
            topic_id="TS007A",
            title="Age by five-year age bands",
            value_column_count=len(TS007A_LABELS),
        )
        client, _, _ = client_for([payload], topic=topic)

        with self.assertRaisesRegex(NomisError, "must not contain inferred"):
            with client.open_topic(topic):
                pass

    def test_ts007a_header_is_the_exact_reviewed_exception(self) -> None:
        payload = make_archive(
            topic_id="TS007A",
            title="Age by five-year age bands",
            include_metadata=False,
        )
        topic = pinned_topic(
            payload,
            topic_id="TS007A",
            title="Age by five-year age bands",
        )
        client, _, _ = client_for([payload], topic=topic)

        with self.assertRaisesRegex(NomisError, "unsupported TS007A value headers"):
            with client.open_topic(topic):
                pass

    def test_malformed_metadata_is_rejected(self) -> None:
        cases = (
            (
                b"Title: A title\nVersion: 1\nArea Type\n",
                None,
                "missing required fields",
            ),
            (
                metadata_bytes(
                    "Number of usual residents in households and communal "
                    "establishments"
                ),
                "metadata/ts001-2021-2.txt",
                "filename/version mismatch",
            ),
            (b"\xff\xfe", None, "metadata is not UTF-8"),
        )
        for metadata_payload, metadata_name, message in cases:
            with self.subTest(message=message):
                payload = make_archive(
                    metadata_payload=metadata_payload,
                    metadata_name=metadata_name,
                )
                self._assert_archive_error(payload, message)

    def test_oversized_uncompressed_member_is_rejected(self) -> None:
        csv_payload = csv_bytes([["2021", "E00000001", "E00000001", "1", "2", "3"]]) + (
            b"\n" * 50_000
        )
        payload = make_archive(
            csv_payload=csv_payload,
            compression=zipfile.ZIP_DEFLATED,
        )
        topic = pinned_topic(payload)
        limit = len(payload) + 100
        self.assertGreater(len(csv_payload), limit)
        client, _, _ = client_for(
            [payload],
            topic=topic,
            max_archive_bytes=limit,
        )

        with self.assertRaisesRegex(NomisError, "ZIP member.*too large"):
            with client.open_topic(topic):
                pass


class NomisCsvValidationTests(unittest.TestCase):
    def _assert_csv_error(
        self,
        *,
        rows: list[list[str]],
        message: str,
        labels: tuple[str, ...] = VALUE_LABELS,
        prefix: tuple[str, ...] = ("date", "geography", "geography code"),
        expected_count: int = 2,
    ) -> None:
        payload = make_archive(
            csv_payload=csv_bytes(rows, labels=labels, prefix=prefix)
        )
        topic = pinned_topic(payload)
        client, _, _ = client_for(
            [payload],
            topic=topic,
            expected_count=expected_count,
        )
        with self.assertRaisesRegex(NomisError, message):
            with client.open_topic(topic) as stream:
                list(stream.rows)

    def test_header_contract_is_closed(self) -> None:
        valid_rows = [
            ["2021", "E00000001", "E00000001", "1", "2", "3"],
            ["2021", "E00000002", "E00000002", "4", "5", "6"],
        ]
        standard_prefix = ("date", "geography", "geography code")
        cases = (
            (
                ("year", "geography", "geography code"),
                VALUE_LABELS,
                "unsupported OA CSV header prefix",
            ),
            (standard_prefix, VALUE_LABELS[:2], "value-column mismatch"),
            (
                standard_prefix,
                (VALUE_LABELS[0], VALUE_LABELS[0], VALUE_LABELS[2]),
                "duplicate headers",
            ),
            (
                standard_prefix,
                ("Unsupported", VALUE_LABELS[1], VALUE_LABELS[2]),
                "unsupported value headers",
            ),
        )
        for prefix, labels, message in cases:
            with self.subTest(message=message):
                self._assert_csv_error(
                    rows=valid_rows,
                    prefix=prefix,
                    labels=labels,
                    message=message,
                )

    def test_invalid_geographies_dates_width_and_duplicates_are_rejected(self) -> None:
        cases = (
            (
                [["2021", "E123", "E123", "1", "2", "3"]],
                "invalid geography",
            ),
            (
                [["2021", "Different", "E00000001", "1", "2", "3"]],
                "invalid geography",
            ),
            (
                [["2020", "E00000001", "E00000001", "1", "2", "3"]],
                "unsupported date",
            ),
            (
                [["2021", "E00000001", "E00000001", "1", "2"]],
                "has 5 columns",
            ),
            (
                [
                    ["2021", "E00000001", "E00000001", "1", "2", "3"],
                    ["2021", "E00000001", "E00000001", "4", "5", "6"],
                ],
                "duplicate OA code",
            ),
        )
        for rows, message in cases:
            with self.subTest(message=message):
                self._assert_csv_error(
                    rows=rows,
                    message=message,
                    expected_count=1,
                )

    def test_invalid_non_finite_negative_and_untrimmed_numbers_are_rejected(
        self,
    ) -> None:
        for invalid, message in (
            ("", "invalid numeric"),
            ("not-a-number", "invalid numeric"),
            ("NaN", "non-finite numeric"),
            ("Infinity", "non-finite numeric"),
            ("-1", "negative numeric"),
            (" 1", "invalid numeric"),
            ("1_000", "invalid numeric"),
        ):
            with self.subTest(invalid=invalid):
                self._assert_csv_error(
                    rows=[
                        [
                            "2021",
                            "E00000001",
                            "E00000001",
                            invalid,
                            "2",
                            "3",
                        ]
                    ],
                    message=message,
                    expected_count=1,
                )

    def test_exact_england_count_is_enforced_after_wales_is_filtered(self) -> None:
        self._assert_csv_error(
            rows=[
                ["2021", "W00000001", "W00000001", "1", "2", "3"],
                ["2021", "E00000001", "E00000001", "4", "5", "6"],
            ],
            message="England OA row-count mismatch.*expected 2, received 1",
            expected_count=2,
        )


if __name__ == "__main__":
    unittest.main()
