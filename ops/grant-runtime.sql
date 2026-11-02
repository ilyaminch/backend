-- Run against the application DB as its owner, after creating gis_runtime
-- interactively with a private password. Do not use the runtime role for migrations.
GRANT CONNECT ON DATABASE gis TO gis_runtime;
GRANT USAGE ON SCHEMA public TO gis_runtime;
GRANT SELECT ON categories, data_sources, gis_objects, regions,
    dataset_state, layer_state, users, auth_sessions, alembic_version TO gis_runtime;
GRANT INSERT, UPDATE ON auth_sessions TO gis_runtime;
-- ZTOZ-107: whole-layer replacement and new source definitions, no DDL.
GRANT INSERT ON data_sources TO gis_runtime;
GRANT INSERT, DELETE ON gis_objects, regions TO gis_runtime;
GRANT UPDATE ON dataset_state, layer_state TO gis_runtime;
-- ZTOZ-115: replace trusted demo dictionaries after deleting their geodata.
GRANT DELETE ON data_sources TO gis_runtime;
GRANT INSERT, DELETE ON categories TO gis_runtime;
-- User provisioning CLI still needs the migrator connection. Runtime cannot
-- change users or run DDL. Dictionary replacement is guarded by admin/CSRF.
