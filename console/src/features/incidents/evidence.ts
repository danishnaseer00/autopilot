import type { EvidenceRef, IncidentDetail } from "../../api/types";

export interface EvidenceSlot {
  evidence: EvidenceRef;
  /** 1-based index used by `GET /incidents/{id}/evidence/{n}`. */
  n: number;
  source: "ranked_cause" | "diagnosis_claim";
  causeRank?: number;
  claim?: string;
}

/**
 * SPEC-36 §3.7 numbers evidence as a single flat list. The mock and the
 * incident screen agree on this order: ranked causes (rank ascending) first,
 * then diagnosis claims in order.
 */
export function flattenEvidence(detail: IncidentDetail): EvidenceSlot[] {
  const causes = [...detail.causes].sort((a, b) => a.rank - b.rank);
  const slots: EvidenceSlot[] = [];
  for (const cause of causes) {
    for (const evidence of cause.evidence) {
      slots.push({ evidence, n: slots.length + 1, source: "ranked_cause", causeRank: cause.rank });
    }
  }
  for (const claim of detail.diagnosis.claims) {
    for (const evidence of claim.evidence) {
      slots.push({
        evidence,
        n: slots.length + 1,
        source: "diagnosis_claim",
        claim: claim.claim,
      });
    }
  }
  return slots;
}

export function evidenceLabel(n: number): string {
  return `Evidence citation ${n}`;
}
