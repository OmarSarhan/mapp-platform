WITH scoped AS (
  SELECT
    oa21cd AS oa_id,
    ts001_0001 AS population,
    geom_3857::geometry(MultiPolygon, 3857) AS geom_3857
  FROM source_census.census_2021_england_oa
  WHERE ts001_0001 IS NOT NULL
    AND geom_3857 && ST_Transform(ST_MakeEnvelope(-1.85, 53.65, -1.2, 54.0, 4326), 3857)
    AND ST_Intersects(geom_3857, ST_Transform(ST_MakeEnvelope(-1.85, 53.65, -1.2, 54.0, 4326), 3857))
)
SELECT
  oa_id,
  population,
  ntile(5) OVER (ORDER BY population, oa_id) AS population_quintile,
  'OA ' || oa_id || ' · Population ' || to_char(population, 'FM999,999,999,990') AS hover_label,
  geom_3857
FROM scoped
