-- postgres_baseline seed — the named baseline `app-db` resets to at the
-- start of every trial (see project.yaml: assets.seeds.postgres_baseline
-- and default_environment.services.app-db.reset).
--
-- Data-only and idempotent by design. The schema, role grants, and table
-- are created once by shared/app-db/init.sql before PostgREST connects;
-- keeping this seed free of DDL leaves PostgREST's start-up schema cache
-- valid, so `GET /widgets` stays readable after the reset applies.
--
-- Applied by the sql_dump recipe as `psql -U ${POSTGRES_USER} -d
-- ${POSTGRES_DB}` inside the app-db container — i.e. as the `authenticator`
-- owner role. It overwrites init.sql's `factory_default` row with
-- `baseline`; the agent reading `baseline` back over the REST API is the
-- observable proof that the recipe fired.
INSERT INTO api.widgets (id, name) VALUES (1, 'baseline')
  ON CONFLICT (id) DO UPDATE SET name = 'baseline';
