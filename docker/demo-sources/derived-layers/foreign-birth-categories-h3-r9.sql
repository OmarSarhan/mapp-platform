WITH candidate_ids AS (
  SELECT DISTINCT generated.cell AS h3
  FROM _mapp_h3_scope
  CROSS JOIN LATERAL h3_polygon_to_cells_experimental(
    _mapp_h3_scope.geom_4326,
    9,
    'overlapping'
  ) AS generated(cell)
),
cells_4326 AS MATERIALIZED (
  SELECT
    candidate.h3,
    public.ST_Multi(
      public.ST_SetSRID(
        public.ST_GeomFromEWKB(h3_cell_to_boundary_wkb(candidate.h3)),
        4326
      )
    )::public.geometry(MultiPolygon, 4326) AS geom_4326
  FROM candidate_ids AS candidate
),
cells AS MATERIALIZED (
  SELECT
    h3,
    geom_4326::public.geography AS geog_4326,
    public.ST_Transform(geom_4326, 3857)
      ::public.geometry(MultiPolygon, 3857) AS geom_3857
  FROM cells_4326
),
source_scope AS MATERIALIZED (
  SELECT public.ST_Transform(_mapp_h3_scope.geom_4326, 3857) AS geom_source
  FROM _mapp_h3_scope
),
bounded_sources AS MATERIALIZED (
  SELECT
    COALESCE(source.ts004_0011::double precision, 0.0) AS africa,
    COALESCE(source.ts004_0012::double precision, 0.0) AS middle_east_asia,
    COALESCE(source.ts004_0013::double precision, 0.0) AS americas_caribbean,
    COALESCE(source.ts004_0014::double precision, 0.0) AS antarctica_oceania_other,
    COALESCE(source.ts004_0015::double precision, 0.0) AS british_overseas,
    source.geom_3857 AS source_geom
  FROM source_census.census_2021_england_oa AS source
  WHERE source.geom_3857 IS NOT NULL
    AND EXISTS (
      SELECT 1
      FROM source_scope
      WHERE source.geom_3857 && source_scope.geom_source
        AND public.ST_Intersects(source.geom_3857, source_scope.geom_source)
    )
),
geodesic_sources AS MATERIALIZED (
  SELECT
    africa,
    middle_east_asia,
    americas_caribbean,
    antarctica_oceania_other,
    british_overseas,
    public.ST_Transform(source_geom, 4326)::public.geography AS source_geog_4326
  FROM bounded_sources
),
measured_sources AS MATERIALIZED (
  SELECT
    africa,
    middle_east_asia,
    americas_caribbean,
    antarctica_oceania_other,
    british_overseas,
    source_geog_4326,
    public.ST_Area(source_geog_4326, true) AS source_area_m2
  FROM geodesic_sources
  WHERE source_geog_4326 IS NOT NULL
),
matched_pairs AS MATERIALIZED (
  SELECT
    cell.h3,
    cell.geog_4326,
    cell.geom_3857,
    source.africa,
    source.middle_east_asia,
    source.americas_caribbean,
    source.antarctica_oceania_other,
    source.british_overseas,
    source.source_geog_4326,
    source.source_area_m2
  FROM cells AS cell
  JOIN measured_sources AS source
    ON source.source_geog_4326 && cell.geog_4326
   AND public.ST_Intersects(source.source_geog_4326, cell.geog_4326)
),
pair_areas AS MATERIALIZED (
  SELECT
    h3,
    geom_3857,
    africa,
    middle_east_asia,
    americas_caribbean,
    antarctica_oceania_other,
    british_overseas,
    source_area_m2,
    public.ST_Area(
      public.ST_Intersection(source_geog_4326, geog_4326),
      true
    ) AS intersection_area_m2
  FROM matched_pairs
),
weighted_cells AS (
  SELECT
    h3,
    geom_3857,
    SUM(africa * intersection_area_m2 / NULLIF(source_area_m2, 0.0))
      ::double precision AS africa_estimate,
    SUM(middle_east_asia * intersection_area_m2 / NULLIF(source_area_m2, 0.0))
      ::double precision AS middle_east_asia_estimate,
    SUM(americas_caribbean * intersection_area_m2 / NULLIF(source_area_m2, 0.0))
      ::double precision AS americas_caribbean_estimate,
    SUM(antarctica_oceania_other * intersection_area_m2 / NULLIF(source_area_m2, 0.0))
      ::double precision AS antarctica_oceania_other_estimate,
    SUM(british_overseas * intersection_area_m2 / NULLIF(source_area_m2, 0.0))
      ::double precision AS british_overseas_estimate
  FROM pair_areas
  WHERE source_area_m2 > 0.0
    AND intersection_area_m2 > 0.0
  GROUP BY h3, geom_3857
),
classified AS (
  SELECT
    *,
    CASE
      WHEN africa_estimate >= middle_east_asia_estimate
       AND africa_estimate >= americas_caribbean_estimate
       AND africa_estimate >= antarctica_oceania_other_estimate
       AND africa_estimate >= british_overseas_estimate
        THEN 'Africa'
      WHEN middle_east_asia_estimate >= americas_caribbean_estimate
       AND middle_east_asia_estimate >= antarctica_oceania_other_estimate
       AND middle_east_asia_estimate >= british_overseas_estimate
        THEN 'Middle East & Asia'
      WHEN americas_caribbean_estimate >= antarctica_oceania_other_estimate
       AND americas_caribbean_estimate >= british_overseas_estimate
        THEN 'Americas & Caribbean'
      WHEN antarctica_oceania_other_estimate >= british_overseas_estimate
        THEN 'Antarctica, Oceania & Other'
      ELSE 'British Overseas'
    END AS most_common_category
  FROM weighted_cells
  WHERE GREATEST(
    africa_estimate,
    middle_east_asia_estimate,
    americas_caribbean_estimate,
    antarctica_oceania_other_estimate,
    british_overseas_estimate
  ) > 0.0
)
SELECT
  h3::text AS h3_id,
  9::smallint AS h3_resolution,
  ROUND(africa_estimate::numeric, 1)::double precision AS africa_estimate,
  ROUND(middle_east_asia_estimate::numeric, 1)::double precision
    AS middle_east_asia_estimate,
  ROUND(americas_caribbean_estimate::numeric, 1)::double precision
    AS americas_caribbean_estimate,
  ROUND(antarctica_oceania_other_estimate::numeric, 1)::double precision
    AS antarctica_oceania_other_estimate,
  ROUND(british_overseas_estimate::numeric, 1)::double precision
    AS british_overseas_estimate,
  most_common_category,
  geom_3857::public.geometry(MultiPolygon, 3857) AS geom_3857
FROM classified
