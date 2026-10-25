import { humanize } from "../lib/format";

export type BadgeTone = "success" | "warning" | "danger" | "info" | "neutral";

interface BadgeSpec {
  label: string;
  tone: BadgeTone;
}

const BADGES: Record<string, BadgeSpec> = {
  // Project status (SPEC-36 §3.2 / SPEC-04)
  active: { label: "Active", tone: "success" },
  suspended: { label: "Suspended", tone: "danger" },
  orphaned: { label: "Orphaned", tone: "warning" },
  credential_invalid: { label: "Repo credential invalid", tone: "danger" },

  // Deployment status (SPEC-04 §3.3)
  queued: { label: "Queued", tone: "info" },
  building: { label: "Building", tone: "warning" },
  ready: { label: "Ready", tone: "success" },
  live: { label: "Live", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
  cancelled: { label: "Cancelled", tone: "neutral" },
  superseded: { label: "Superseded", tone: "neutral" },

  // Incident status / severity (SPEC-36 §3.7)
  open: { label: "Open", tone: "danger" },
  diagnosed: { label: "Diagnosed", tone: "warning" },
  actioning: { label: "Actioning", tone: "info" },
  resolved: { label: "Resolved", tone: "success" },
  escalated: { label: "Escalated", tone: "danger" },
  critical: { label: "Critical", tone: "danger" },
  high: { label: "High", tone: "danger" },
  medium: { label: "Medium", tone: "warning" },
  low: { label: "Low", tone: "info" },

  // Domain status (SPEC-36 §3.5)
  pending: { label: "Pending verification", tone: "warning" },
  verified: { label: "Verified", tone: "info" },

  // Secret targets (SPEC-18 R-5); build visibility is alarming on purpose.
  build: { label: "Build-visible", tone: "danger" },
  runtime: { label: "Runtime only", tone: "neutral" },
  both: { label: "Build and runtime", tone: "danger" },

  // Misc
  approved: { label: "Approved", tone: "info" },
  rejected: { label: "Rejected", tone: "danger" },
  executed: { label: "Executed", tone: "success" },
  proposed: { label: "Proposed", tone: "info" },
  pending_approval: { label: "Awaiting approval", tone: "warning" },
  production: { label: "Production", tone: "info" },
  preview: { label: "Preview", tone: "neutral" },
};

export function badgeSpec(status: string): BadgeSpec {
  return BADGES[status] ?? { label: humanize(status), tone: "neutral" };
}

export function StatusBadge({
  status,
  srPrefix,
}: {
  status: string;
  srPrefix?: string;
}) {
  const spec = badgeSpec(status);
  return (
    <span className={`badge badge--${spec.tone}`}>
      <span className="badge__dot" aria-hidden="true" />
      {srPrefix ? <span className="visually-hidden">{srPrefix}: </span> : null}
      {spec.label}
    </span>
  );
}
