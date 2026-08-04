# Third-party and data notices

The MAPP Platform project licence is in `LICENSE`. It does not replace the
terms of the components and data sources below.

## GEOLYTIX XYZ

The XYZ container builds GEOLYTIX XYZ v4.23.4 at commit
`a6f03c07dd7aaae2e9ab04087143ee0400e15cb9` from
<https://github.com/GEOLYTIX/xyz>.

Copyright (c) 2024 GEOLYTIX

GEOLYTIX XYZ is licensed under the MIT License reproduced in `LICENSE`; for
this component, retain the GEOLYTIX copyright notice together with that MIT
permission and warranty notice.

## Optional ETL source data

The repository contains ETL code, source URLs, validation pins, and field
mappings. It does not contain the downloaded records. ETL outputs remain under
the applicable publisher and third-party data terms and are not relicensed by
MAPP's MIT licence.

### Leeds sample layers

- Leeds Bus Stops:
  <https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Transportation/MapServer/0>
- Definitive Paths:
  <https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/PROW/MapServer/4>
- Smoke Control Orders:
  <https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/8>

Leeds City Council's open-data terms require attribution for reused council
data and distinguish Open Government Licence data from the smaller set released
under non-commercial terms. Preserve the title, Leeds City Council copyright,
publication year, source link, and the licence attached to the source record.
The Smoke Control Orders ArcGIS metadata additionally states:

> Leeds City Council; Ordnance Survey Crown copyright (C)

Preserve that source-provided notice with any retained or published snapshot.

### Census 2021 and Output Area boundaries

The optional census manifest uses ONS Census 2021 topic summaries obtained
through Nomis and the ONS Output Areas (December 2021) Boundaries EW BGC (V2)
product. Preserve the publisher-supplied source metadata and the attribution:

> Source: Office for National Statistics licensed under the Open Government Licence v.3.0

The boundary product also requires the Ordnance Survey statement documented in
`etl/README.md`. Its authoritative copyright year has not been established, so
do not publish a placeholder or redistribute a boundary snapshot until the
complete product-specific statement is recorded.

## Packaged dependencies and images

Language packages, system packages, base images, map styles, fonts, and hosted
resources retain their own licences and notices. Release SBOM/provenance output
is the authoritative generated inventory for the exact shipped artifacts.
