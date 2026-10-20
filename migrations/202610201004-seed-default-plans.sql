-- target-db: autopilot_platform
-- Seed: the default free plan referenced by orgs.plan_id and SPEC-20/10.
-- limits shape is PROVISIONAL until SPEC-10 is reviewed (finding logged);
-- the ON CONFLICT guard keeps the seed idempotent across re-runs.

INSERT INTO plans (code, name, price_cents, limits, is_default)
VALUES
  ('free', 'Free', 0,
   '{"buildsPerDay": 50, "parallelBuilds": 1, "maxServices": 5,
     "maxCpu": "2", "maxMemory": "4Gi", "historyDays": 7}'::jsonb,
   true),
  ('pro', 'Pro', 2000,
   '{"buildsPerDay": 500, "parallelBuilds": 3, "maxServices": 20,
     "maxCpu": "8", "maxMemory": "16Gi", "historyDays": 30}'::jsonb,
   false)
ON CONFLICT (code) DO NOTHING;
