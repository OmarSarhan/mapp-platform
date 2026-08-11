CREATE EXTENSION IF NOT EXISTS h3;
CREATE EXTENSION IF NOT EXISTS h3_postgis CASCADE;

DO $mapp$
DECLARE
  routine_oid pg_catalog.oid;
  hardened_count pg_catalog.int4 := 0;
BEGIN
  PERFORM pg_catalog.set_config('search_path', 'pg_catalog', true);
  FOR routine_oid IN
    SELECT routine.oid
    FROM pg_catalog.pg_proc AS routine
    JOIN pg_catalog.pg_namespace AS routine_namespace
      ON routine_namespace.oid = routine.pronamespace
    JOIN pg_catalog.pg_depend AS extension_membership
      ON extension_membership.classid =
           'pg_catalog.pg_proc'::pg_catalog.regclass
     AND extension_membership.objid = routine.oid
     AND extension_membership.refclassid =
           'pg_catalog.pg_extension'::pg_catalog.regclass
     AND extension_membership.deptype = 'e'
    JOIN pg_catalog.pg_extension AS extension
      ON extension.oid = extension_membership.refobjid
    WHERE extension.extname = 'h3_postgis'
      AND extension.extnamespace = routine.pronamespace
      AND routine_namespace.nspname = 'public'
      AND routine.oid = ANY(ARRAY[
        'public.h3_polygon_to_cells(public.geometry,pg_catalog.int4)'
          ::pg_catalog.regprocedure,
        'public.h3_polygon_to_cells(public.geography,pg_catalog.int4)'
          ::pg_catalog.regprocedure,
        'public.h3_polygon_to_cells_experimental(public.geometry,pg_catalog.int4,pg_catalog.text)'
          ::pg_catalog.regprocedure,
        'public.h3_polygon_to_cells_experimental(public.geography,pg_catalog.int4,pg_catalog.text)'
          ::pg_catalog.regprocedure,
        'public.h3_lat_lng_to_cell(public.geometry,pg_catalog.int4)'
          ::pg_catalog.regprocedure,
        'public.h3_lat_lng_to_cell(public.geography,pg_catalog.int4)'
          ::pg_catalog.regprocedure,
        'public.h3_latlng_to_cell(public.geometry,pg_catalog.int4)'
          ::pg_catalog.regprocedure,
        'public.h3_latlng_to_cell(public.geography,pg_catalog.int4)'
          ::pg_catalog.regprocedure,
        'public.h3_cell_to_geometry(public.h3index)'
          ::pg_catalog.regprocedure,
        'public.h3_cell_to_geography(public.h3index)'
          ::pg_catalog.regprocedure,
        'public.h3_cell_to_boundary_geometry(public.h3index)'
          ::pg_catalog.regprocedure,
        'public.h3_cell_to_boundary_geography(public.h3index)'
          ::pg_catalog.regprocedure,
        'public.h3_cell_to_boundary_geometry(public.h3index,pg_catalog.bool)'
          ::pg_catalog.regprocedure,
        'public.h3_cell_to_boundary_geography(public.h3index,pg_catalog.bool)'
          ::pg_catalog.regprocedure,
        'public.h3_cells_to_multi_polygon_geometry(public.h3index[])'
          ::pg_catalog.regprocedure,
        'public.h3_cells_to_multi_polygon_geography(public.h3index[])'
          ::pg_catalog.regprocedure
      ])
  LOOP
    EXECUTE pg_catalog.format(
      'ALTER FUNCTION %s SET search_path = pg_catalog, public',
      routine_oid::pg_catalog.regprocedure
    );
    hardened_count := hardened_count + 1;
  END LOOP;
  IF hardened_count <> 16 THEN
    RAISE EXCEPTION
      'Expected sixteen public h3_postgis SQL wrappers, found %',
      hardened_count;
  END IF;
END
$mapp$;
