-- target-db: autopilot_platform
-- SPEC-03 §4.1 (CITEXT), ADR-6 (TimescaleDB). Applied as autopilot_migrator.
-- citext + pgcrypto are mandatory; timescaledb is preferred but optional
-- (aborting on it would make every dev machine need the preload library
-- before any schema could be applied - ADR-6 remains the target, conversion
-- of usage_daily to a hypertable can be a later forward-only migration).

DO $req$
BEGIN
  EXECUTE 'CREATE EXTENSION IF NOT EXISTS citext';
  EXECUTE 'CREATE EXTENSION IF NOT EXISTS pgcrypto';
EXCEPTION
  WHEN OTHERS THEN
    RAISE EXCEPTION 'required extension failed to install: %', SQLERRM;
END;
$req$;

DO $ts$
BEGIN
  EXECUTE 'CREATE EXTENSION IF NOT EXISTS timescaledb';
EXCEPTION
  WHEN OTHERS THEN
    RAISE NOTICE 'timescaledb unavailable (%); continuing as plain Postgres', SQLERRM;
END;
$ts$;
