# Licensing status

No project-level licence has been selected for MAPP Platform. This repository
therefore does not include a `LICENSE` file, and its contents should not be
treated as licensed for redistribution or reuse until the owner makes and
records that decision.

This file is informational and is not a licence.

## Decisions required from the owner

1. Select a licence for original platform source and documentation.
2. Confirm whether contributions will use the same licence and whether a
   contributor agreement or developer certificate is required.
3. Identify the author, source, and permitted use of every SVG under
   `instance/public/svg`.
4. Confirm the terms, attribution, and redistribution position for each Leeds
   dataset configured by the ETL and displayed by the workspace.
5. Produce a third-party notices inventory for container images, Python and
   Node dependencies, the pinned GEOLYTIX XYZ build, map tiles, fonts, styles,
   and other runtime resources.
6. Decide whether generated screenshots, schemas, example workspaces, and
   sample data have separate licensing or privacy constraints.

## Known third-party elements

- The XYZ image is built from the upstream GEOLYTIX XYZ repository at the
  pinned v4.23.4 commit. That source revision contains an MIT licence. Preserve
  its licence text and required notices in image notices and distributions;
  this verification does not license MAPP's original source or bundled assets.
- PostgreSQL/PostGIS, Caddy, Playwright, React, Vite, Python packages, Node
  packages, and base images carry their own terms.
- Leeds ArcGIS endpoints being publicly reachable does not by itself grant
  unrestricted redistribution. Review the publisher's specific open-data
  entry, terms, attribution, and commercial-use conditions.
- Hosted styles, basemaps, and other external resources may have terms
  independent of this repository.

## Asset provenance record

Before release, record at least:

| Path or component | Author/source | Licence or permission | Required attribution | Reviewed by/date |
| --- | --- | --- | --- | --- |
| `instance/public/svg/*.svg` | To be established | To be established | To be established | Pending |
| ETL-configured Leeds layers | Leeds publisher entries | To be confirmed per layer | To be confirmed | Pending |
| GEOLYTIX XYZ | Upstream repository, pinned v4.23.4 commit | MIT at the pinned commit | Preserve upstream MIT notice | Commit terms verified; release notice packaging pending |
| Other runtime dependencies | Dependency manifests/images | Generate notices inventory | Per dependency | Pending |

Do not add a project licence or assert asset provenance by inference. The owner
must approve the final choice and supporting evidence.
