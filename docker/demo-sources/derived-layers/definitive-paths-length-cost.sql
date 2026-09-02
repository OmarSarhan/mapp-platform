WITH scoped_paths AS (
  SELECT
    object_id,
    path_name,
    length_metres,
    width_metres,
    geom_3857
  FROM source_ops.definitive_paths
  WHERE geom_3857 && ST_Transform(
    ST_MakeEnvelope(-1.85, 53.65, -1.2, 54.0, 4326),
    3857
  )
    AND length_metres IS NOT NULL
)
SELECT
  object_id,
  COALESCE(NULLIF(BTRIM(path_name), ''), 'Unnamed path') AS path_name_display,
  length_metres,
  width_metres,
  (length_metres * width_metres * 30)::double precision AS estimated_cost_gbp,
  CASE
    WHEN width_metres IS NULL THEN NULL
    ELSE '£' || to_char(length_metres * width_metres * 30, 'FM999,999,999,990')
  END AS estimated_cost_display,
  ntile(5) OVER (ORDER BY length_metres, object_id) AS length_quintile,
  ST_Multi(geom_3857)::geometry(MultiLineString, 3857) AS geom_3857
FROM scoped_paths
