CREATE TABLE IF NOT EXISTS public.mapp_platform_layer_dependencies (
  alias text NOT NULL,
  relation text NOT NULL,
  CONSTRAINT mapp_platform_layer_dependencies_pkey
    PRIMARY KEY (alias, relation),
  CONSTRAINT mapp_platform_layer_dependencies_relation_format
    CHECK (relation ~ '^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)?$')
);

CREATE OR REPLACE FUNCTION public.mapp_sync_platform_layer_dependencies(
  p_alias text,
  p_relations jsonb
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $mapp_platform_sync$
BEGIN
  IF p_alias IS NULL OR btrim(p_alias) = '' THEN
    RAISE EXCEPTION 'mapp_sync_platform_layer_dependencies requires a non-empty alias.';
  END IF;

  DELETE FROM public.mapp_platform_layer_dependencies
  WHERE alias = p_alias;

  IF p_relations IS NULL THEN
    RETURN;
  END IF;

  INSERT INTO public.mapp_platform_layer_dependencies (alias, relation)
  SELECT
    p_alias,
    lower(replace(lower(jsonb_array_elements_text(p_relations)), '"', ''))
  WHERE p_relations IS NOT NULL
  ON CONFLICT (alias, relation) DO NOTHING;
END;
$mapp_platform_sync$;

CREATE OR REPLACE FUNCTION public.mapp_block_platform_layer_drops()
RETURNS event_trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $mapp_platform_guard$
DECLARE
  cmd record;
  normalized_relation text;
  object_relation text;
BEGIN
  IF pg_has_role(current_user, 'pg_database_owner', 'MEMBER') THEN
    RETURN;
  END IF;

  FOR cmd IN
    SELECT *
    FROM pg_event_trigger_ddl_commands()
  LOOP
    IF cmd.object_type NOT IN ('table', 'view', 'materialized view') THEN
      CONTINUE;
    END IF;

    object_relation := lower(replace(cmd.object_identity, '"', ''));
    IF object_relation IS NULL OR object_relation = '' THEN
      CONTINUE;
    END IF;
    IF cmd.schema_name IS NOT NULL AND btrim(cmd.schema_name) <> '' THEN
      normalized_relation := format('%s.%s', lower(cmd.schema_name), object_relation);
    ELSE
      normalized_relation := object_relation;
    END IF;

    IF EXISTS (
      SELECT 1
      FROM public.mapp_platform_layer_dependencies
      WHERE lower(relation) = normalized_relation
    ) THEN
      RAISE EXCEPTION USING
        MESSAGE = (
          format(
            'DROP is blocked by active platform references for %s; update the '
            'workspace or dependencies before deleting this relation.',
            normalized_relation
          )
        ),
        ERRCODE = '55006';
    END IF;
  END LOOP;
END;
$mapp_platform_guard$;

DROP EVENT TRIGGER IF EXISTS mapp_block_platform_layer_drops;
CREATE EVENT TRIGGER mapp_block_platform_layer_drops
ON ddl_command_end
WHEN TAG IN ('DROP TABLE', 'DROP VIEW', 'DROP MATERIALIZED VIEW')
EXECUTE FUNCTION public.mapp_block_platform_layer_drops();

GRANT EXECUTE ON FUNCTION public.mapp_sync_platform_layer_dependencies(
  text,
  jsonb
) TO PUBLIC;
