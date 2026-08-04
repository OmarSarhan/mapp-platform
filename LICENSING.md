# Licensing status

MAPP Platform's original source, documentation, configuration, schemas, and
repository-authored sample assets are licensed under the MIT License in
`LICENSE`. The owner selected MIT on 2026-08-04 to match the licence used by
the pinned GEOLYTIX XYZ project. Unless stated otherwise, contributions are
accepted under the same licence; no separate contributor agreement or
developer certificate has been selected.

The project licence does not relicense third-party software, hosted resources,
or data downloaded by the optional ETL. Their own terms and notices continue
to apply. `THIRD_PARTY_NOTICES.md` records the notices relevant to the pinned
XYZ source and configured sample-data sources.

## Known third-party elements

- The XYZ image is built from the upstream GEOLYTIX XYZ repository at the
  pinned v4.23.4 commit. That source revision contains an MIT licence. Preserve
  its licence text and copyright notice in image notices and distributions.
- PostgreSQL/PostGIS, Caddy, Playwright, React, Vite, Python packages, Node
  packages, and base images carry their own terms.
- The configuration service pins `pglast` 8.4, distributed under GPL-3.0-or-
  later. Include its licence and source/notice obligations in the container
  notices review before redistribution.
- The Leeds ETL is optional sample-data provisioning and versions source URLs
  and field mappings, not downloaded records. The owner has classified those
  inputs as open test data. Leeds City Council's published-data terms and each
  source's own metadata still govern downloaded snapshots and attribution;
  the Smoke Control Orders source carries an explicit Ordnance Survey notice.
- Standard ONS Census 2021 outputs are published under the Open Government
  Licence v3.0. The configured Output Area boundary product also contains
  Ordnance Survey rights and must retain the attribution statement supplied
  with that product. These source terms remain separate from the project MIT
  licence.
- The OS copyright year for the configured boundary product is not yet
  evidenced in this repository. The `[year]` placeholder must not appear in a
  released display: exposing or redistributing the boundary data remains
  blocked until the authoritative year is recorded and the complete ONS/OS
  attribution is configured. Do not infer that year.
- Hosted styles, basemaps, and other external resources may have terms
  independent of this repository.

## Asset provenance record

| Path or component | Author/source | Licence or permission | Required attribution | Reviewed by/date |
| --- | --- | --- | --- | --- |
| `instance/public/svg/*.svg` | Repository-authored sample icons, introduced by the repository owner in commit `58cb177` | Project MIT licence | None beyond the project licence | Repository history reviewed 2026-08-04 |
| `instance/etl/layers.json` Leeds sample sources | Leeds City Council ArcGIS services; records are fetched at runtime and are not versioned here | Publisher terms; owner-classified open test data | Preserve source URL and Leeds attribution; Smoke Control Orders additionally reports `Leeds City Council; Ordnance Survey Crown copyright (C)` | Manifest and live source metadata reviewed 2026-08-04 |
| `instance/etl/census.json` Census 2021 statistics | Office for National Statistics through Nomis | Open Government Licence v3.0 | Source: Office for National Statistics licensed under the Open Government Licence v3.0 | Source terms reviewed 2026-07-26 |
| `instance/etl/census.json` OA21 BGC V2 boundaries | Office for National Statistics; contains Ordnance Survey data | Product-specified OGL/third-party rights | Retain the ONS and OS attribution supplied with the product, including its authoritative copyright year | Source terms reviewed 2026-07-26; attribution year unresolved, display/redistribution blocked |
| GEOLYTIX XYZ | Upstream repository, pinned v4.23.4 commit | MIT at the pinned commit | Preserve upstream copyright and MIT notice | Commit terms and repository notice reviewed 2026-08-04 |
| Other runtime dependencies | Dependency manifests/images | Generate notices inventory | Per dependency | Pending |

Generated screenshots and ETL outputs are not relicensed by the project MIT
licence. Review their data, privacy, and source-attribution obligations before
publishing them. A generated dependency/image notice inventory remains part of
the release SBOM and provenance work rather than a manually maintained list in
this file.
