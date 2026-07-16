CREATE TABLE IF NOT EXISTS public.instance_runtime (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  initialized_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  postgres_version text NOT NULL DEFAULT current_setting('server_version'),
  postgis_version text NOT NULL DEFAULT postgis_full_version()
);

INSERT INTO public.instance_runtime (singleton)
VALUES (true)
ON CONFLICT (singleton) DO NOTHING;

