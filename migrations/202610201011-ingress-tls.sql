-- target-db: autopilot_platform
-- SPEC-15 ingress and TLS persistence.
--
-- Findings / amendments applied here:
--   * ``domains.status`` needed a ``suspended`` state.  R-4 suspends a domain
--     when its DNS proof disappears; the baseline CHECK only allowed
--     pending/verified/active/failed.  A suspended domain keeps its rows (and
--     certificate history) but the ingress renderer never includes it.
--   * R-2 says issued certificates are tracked in ``usage_daily``.  The
--     baseline table had no counter, so add one.  The authoritative budget
--     ledger is ``ingress_certificates`` because AC-2 is per registered domain
--     and a retry must not create a second charge; ``usage_daily`` is the
--     reporting rollup.
--   * ``ingress_certificates`` is the durable certificate lifecycle record.
--     ``domains.cert_*`` stays the tenant-facing projection for custom
--     hostnames; the wildcard preview certificate has no domain row.
--   * ``ingress_outbox`` is the SPEC-15 durable retry queue.  It is
--     platform-owned, append-only-by-key and carries only C3 hostnames/ids,
--     never secret material.  Like ``route_history`` it is not exposed to the
--     tenant role; the runtime reconciler uses the platform role.  Revoking the
--     default app grant makes that boundary a grant, not a code review.
--
-- SPEC-03 §9: forward-only.  No existing row is rewritten.

ALTER TABLE domains
  DROP CONSTRAINT IF EXISTS domains_status_check;
ALTER TABLE domains
  ADD CONSTRAINT domains_status_check
  CHECK (status IN ('pending','verified','active','failed','suspended'));

ALTER TABLE domains
  ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMPTZ;

ALTER TABLE usage_daily
  ADD COLUMN IF NOT EXISTS certificates_issued INT NOT NULL DEFAULT 0
    CHECK (certificates_issued >= 0);

CREATE TABLE ingress_certificates (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id            UUID,                    -- NULL for the platform wildcard
  registered_domain TEXT NOT NULL,
  hostnames         TEXT[] NOT NULL,
  is_wildcard       BOOLEAN NOT NULL DEFAULT false,
  status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','issued','failed','revoked')),
  idempotency_key   TEXT NOT NULL,
  issuer            TEXT NOT NULL,
  serial            TEXT,
  issued_at         TIMESTAMPTZ,
  expires_at        TIMESTAMPTZ,
  revoked_at        TIMESTAMPTZ,
  last_error        TEXT,
  data_class        TEXT NOT NULL DEFAULT 'C3'
                      CHECK (data_class IN ('C1','C2','C3','C4')),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

  CHECK (cardinality(hostnames) > 0),
  UNIQUE (idempotency_key)
);

-- One live wildcard certificate per registered domain is the load-bearing
-- invariant behind SPEC-15 R-1/AC-1.  A partial unique index is the database
-- backstop against two issuance workers racing.
CREATE UNIQUE INDEX ingress_certificates_live_wildcard
  ON ingress_certificates (registered_domain)
  WHERE is_wildcard AND status IN ('pending','issued');

CREATE INDEX ingress_certificates_budget
  ON ingress_certificates (registered_domain, created_at DESC);
CREATE INDEX ingress_certificates_expiry
  ON ingress_certificates (expires_at)
  WHERE status = 'issued';
CREATE INDEX ingress_certificates_hosts
  ON ingress_certificates USING GIN (hostnames);

CREATE TABLE ingress_outbox (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  topic            TEXT NOT NULL,
  idempotency_key  TEXT NOT NULL,
  payload          JSONB NOT NULL,
  status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','running','succeeded','dead')),
  attempts         INT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts     INT NOT NULL DEFAULT 8 CHECK (max_attempts >= 1),
  available_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  lease_expires_at TIMESTAMPTZ,
  worker_id        TEXT,
  last_error       TEXT,
  data_class       TEXT NOT NULL DEFAULT 'C3'
                     CHECK (data_class IN ('C1','C2','C3','C4')),
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

  UNIQUE (topic, idempotency_key)
);

CREATE INDEX ingress_outbox_pending
  ON ingress_outbox (available_at)
  WHERE status = 'pending';
CREATE INDEX ingress_outbox_running
  ON ingress_outbox (lease_expires_at)
  WHERE status = 'running';

-- Platform-owned queues/ledgers.  The tenant role may read its certificate
-- projection through ``domains``; it may not mutate the issuance ledger or
-- enqueue reconciler work directly.
REVOKE ALL ON ingress_certificates FROM autopilot_app;
REVOKE ALL ON ingress_outbox FROM autopilot_app;
