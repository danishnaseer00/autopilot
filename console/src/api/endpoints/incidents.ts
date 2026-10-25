import { request } from "../client";
import { paths } from "../contract";
import type {
  CursorPage,
  Diagnosis,
  DiagnosisClaim,
  EvidenceRef,
  EvidenceResolution,
  Incident,
  IncidentAction,
  IncidentDetail,
  IncidentSeverity,
  IncidentStatus,
  RankedCause,
  TimelineEvent,
} from "../types";

/**
 * Wire shapes returned by the control API. The API keeps pagination and a
 * richer diagnosis envelope; the screens consume the flat SPEC-36 shape.
 */
interface RawIncidentSummary {
  id: string;
  project_id: string;
  deployment_id: string | null;
  status: IncidentStatus;
  severity: IncidentSeverity;
  detected_at: string;
  detected_by: string;
  fault_signature?: string | null;
  resolved_at: string | null;
  resolved_by: string | null;
  recovery_verified: boolean;
  summary?: string | null;
}

interface RawDiagnosis {
  id: string;
  summary: string;
  confidence: number;
  candidates: RankedCause[];
  claims: DiagnosisClaim[];
  model_version: string;
  corpus_version: string;
  created_at: string;
}

interface RawIncidentDetail extends RawIncidentSummary {
  diagnosis: RawDiagnosis | null;
  actions: IncidentAction[];
}

interface RawEvidence {
  kind: EvidenceRef["kind"];
  ref: string;
  resolved: boolean;
  value: unknown;
  unsupported_reason: string | null;
}

interface RawTimelineEvent {
  at: string;
  kind: string;
  title: string;
  detail: string | null;
  actor: string | null;
}

interface RawTimeline {
  items: RawTimelineEvent[];
}

function toIncident(raw: RawIncidentSummary): Incident {
  return {
    id: raw.id,
    project_id: raw.project_id,
    deployment_id: raw.deployment_id,
    status: raw.status,
    severity: raw.severity,
    detected_at: raw.detected_at,
    detected_by: raw.detected_by,
    fault_signature: raw.fault_signature ?? null,
    resolved_at: raw.resolved_at,
    resolved_by: raw.resolved_by,
    recovery_verified: raw.recovery_verified,
  };
}

function toDiagnosis(raw: RawDiagnosis | null): Diagnosis {
  if (!raw) {
    return {
      summary: "No diagnosis was retained for this incident.",
      confidence: 0,
      claims: [],
    };
  }
  return {
    summary: raw.summary,
    confidence: raw.confidence,
    claims: raw.claims ?? [],
    model_version: raw.model_version,
    corpus_version: raw.corpus_version,
  };
}

function toTimelineKind(kind: string): TimelineEvent["kind"] {
  switch (kind) {
    case "diagnosis":
      return "diagnosed";
    case "action":
      return "executed";
    case "resolved":
      return "resolved";
    case "detected":
      return "detected";
    default:
      return "detected";
  }
}

function evidenceSummary(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "The citation resolved without a value.";
  return JSON.stringify(value);
}

/** SPEC-36 A3.7 - AutoPilot incidents, evidence, approvals and timeline. */
export const incidentsApi = {
  list: async (
    org: string,
    filters: { status?: IncidentStatus; severity?: IncidentSeverity } = {},
    signal?: AbortSignal,
  ): Promise<Incident[]> => {
    const page = await request<CursorPage<RawIncidentSummary>>(
      "GET",
      paths.orgIncidents(org),
      {
        query: { status: filters.status, severity: filters.severity },
        signal,
      },
    );
    return page.items.map(toIncident);
  },
  get: async (id: string, signal?: AbortSignal): Promise<IncidentDetail> => {
    const raw = await request<RawIncidentDetail>("GET", paths.incident(id), { signal });
    return {
      ...toIncident(raw),
      causes: raw.diagnosis?.candidates ?? [],
      diagnosis: toDiagnosis(raw.diagnosis),
      actions: raw.actions ?? [],
    };
  },
  evidence: async (
    id: string,
    n: number,
    signal?: AbortSignal,
  ): Promise<EvidenceResolution> => {
    const raw = await request<RawEvidence>("GET", paths.incidentEvidence(id, n), {
      signal,
    });
    return {
      status: raw.resolved ? "resolved" : "unsupported",
      kind: raw.kind,
      ref: raw.ref,
      summary: raw.resolved
        ? evidenceSummary(raw.value)
        : (raw.unsupported_reason ?? "This citation could not be resolved."),
      reason: raw.resolved ? undefined : (raw.unsupported_reason ?? undefined),
    };
  },
  approve: (id: string, actionId: string) =>
    request<IncidentAction>("POST", paths.incidentApprove(id, actionId), { body: {} }),
  reject: (id: string, actionId: string) =>
    request<IncidentAction>("POST", paths.incidentReject(id, actionId), { body: {} }),
  timeline: async (id: string, signal?: AbortSignal): Promise<TimelineEvent[]> => {
    const raw = await request<RawTimeline>("GET", paths.incidentTimeline(id), {
      signal,
    });
    return raw.items.map((event, index) => ({
      id: `${event.kind}-${index}-${event.at}`,
      at: event.at,
      kind: toTimelineKind(event.kind),
      summary: event.detail ? `${event.title}: ${event.detail}` : event.title,
      actor: event.actor ?? "system",
    }));
  },
};
