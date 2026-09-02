WITH scoped AS (
  SELECT
    oa21cd AS oa_id,
    COALESCE(ts004_0011, 0)::double precision AS africa,
    COALESCE(ts004_0012, 0)::double precision AS middle_east_asia,
    COALESCE(ts004_0013, 0)::double precision AS americas_caribbean,
    COALESCE(ts004_0014, 0)::double precision AS antarctica_oceania_other,
    COALESCE(ts004_0015, 0)::double precision AS british_overseas,
    geom_3857::geometry(MultiPolygon, 3857) AS geom_3857
  FROM source_census.census_2021_england_oa
  WHERE geom_3857 && ST_Transform(ST_MakeEnvelope(-1.85, 53.65, -1.2, 54.0, 4326), 3857)
    AND ST_Intersects(geom_3857, ST_Transform(ST_MakeEnvelope(-1.85, 53.65, -1.2, 54.0, 4326), 3857))
)
SELECT
  oa_id, africa, middle_east_asia, americas_caribbean, antarctica_oceania_other, british_overseas,
  CASE WHEN africa BETWEEN 1 AND 3 THEN 1 WHEN africa BETWEEN 4 AND 8 THEN 2 WHEN africa >= 9 THEN 3 END AS africa_band,
  CASE WHEN middle_east_asia BETWEEN 1 AND 4 THEN 1 WHEN middle_east_asia BETWEEN 5 AND 12 THEN 2 WHEN middle_east_asia >= 13 THEN 3 END AS middle_east_asia_band,
  CASE WHEN americas_caribbean BETWEEN 1 AND 2 THEN 1 WHEN americas_caribbean BETWEEN 3 AND 6 THEN 2 WHEN americas_caribbean >= 7 THEN 3 END AS americas_caribbean_band,
  CASE WHEN antarctica_oceania_other = 1 THEN 1 WHEN antarctica_oceania_other = 2 THEN 2 WHEN antarctica_oceania_other >= 3 THEN 3 END AS antarctica_oceania_other_band,
  CASE WHEN british_overseas = 1 THEN 1 WHEN british_overseas = 2 THEN 2 WHEN british_overseas >= 3 THEN 3 END AS british_overseas_band,
  'OA ' || oa_id || ' · Africa ' || to_char(africa, 'FM999,999,999,990') AS africa_hover_label,
  'OA ' || oa_id || ' · Middle East & Asia ' || to_char(middle_east_asia, 'FM999,999,999,990') AS middle_east_asia_hover_label,
  'OA ' || oa_id || ' · Americas & Caribbean ' || to_char(americas_caribbean, 'FM999,999,999,990') AS americas_caribbean_hover_label,
  'OA ' || oa_id || ' · Antarctica, Oceania & Other ' || to_char(antarctica_oceania_other, 'FM999,999,999,990') AS antarctica_oceania_other_hover_label,
  'OA ' || oa_id || ' · British Overseas ' || to_char(british_overseas, 'FM999,999,999,990') AS british_overseas_hover_label,
  geom_3857
FROM scoped
