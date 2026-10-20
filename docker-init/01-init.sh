#!/bin/bash
# Runs once, by the postgres entrypoint, at first initialization.
# Creates SPEC-03 §3 structure: three databases + three roles on one cluster.
#   autopilot_platform  - control plane state (migrator owns DDL)
#   autopilot_telemetry - metric/rollup indexes (data plane + AutoPilot read)
#   autopilot_audit     - audit_log only; platform writes via SECURITY DEFINER
# Roles (SPEC-03 R-2):
#   autopilot_migrator - BYPASSRLS: migrations and documented support tasks only
#   autopilot_app      - NO BYPASSRLS. RLS policies own tenant isolation.
#   autopilot_audit    - INSERT-only access to audit_log, exercised via the
#                        platform_audit_insert() SECURITY DEFINER function.

set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<'SQL'
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'autopilot_migrator') THEN
    CREATE ROLE autopilot_migrator LOGIN BYPASSRLS PASSWORD 'devonly-change-me';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'autopilot_app') THEN
    CREATE ROLE autopilot_app LOGIN PASSWORD 'devonly-change-me';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'autopilot_audit') THEN
    CREATE ROLE autopilot_audit LOGIN PASSWORD 'devonly-change-me';
  END IF;
END $$;

-- create each database if missing; then make the migrator its owner so
-- apply.py (running as autopilot_migrator) can create schema objects.
SELECT format(
  'CREATE DATABASE %I OWNER autopilot_migrator',
  'autopilot_platform')
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'autopilot_platform')
\gexec
SELECT format('ALTER DATABASE %I OWNER TO autopilot_migrator', 'autopilot_platform') \gexec

SELECT format(
  'CREATE DATABASE %I OWNER autopilot_migrator',
  'autopilot_telemetry')
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'autopilot_telemetry')
\gexec
SELECT format('ALTER DATABASE %I OWNER TO autopilot_migrator', 'autopilot_telemetry') \gexec

SELECT format(
  'CREATE DATABASE %I OWNER autopilot_migrator',
  'autopilot_audit')
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'autopilot_audit')
\gexec
SELECT format('ALTER DATABASE %I OWNER TO autopilot_migrator', 'autopilot_audit') \gexec
SQL
echo "01-init.sh: roles + databases ready"
