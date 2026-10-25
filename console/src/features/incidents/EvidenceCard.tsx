import { useEvidence } from "../../hooks/useEvidence";
import { humanize } from "../../lib/format";
import { InlineLoading } from "../../components/Loading";
import { ErrorState } from "../../components/ProblemView";
import type { EvidenceSlot } from "./evidence";

function safeHref(value: string | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value, window.location.origin);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

export function EvidenceCard({
  incidentId,
  slot,
}: {
  incidentId: string;
  slot: EvidenceSlot;
}) {
  const resolution = useEvidence(incidentId, slot.n);
  const href = safeHref(resolution.data?.source_url);

  return (
    <article className="evidence" aria-label={`Evidence ${slot.n}: ${slot.evidence.kind}`}>
      <div className="evidence__head">
        <span className="badge badge--neutral">{humanize(slot.evidence.kind)}</span>
        <span className="evidence__n">#{slot.n}</span>
        <span className="badge badge--neutral">Citation</span>
      </div>

      <dl className="definition-list definition-list--compact">
        <div>
          <dt>Ref</dt>
          <dd>
            <code>{slot.evidence.ref}</code>
          </dd>
        </div>
        {"line_ref" in slot.evidence && slot.evidence.line_ref ? (
          <div>
            <dt>Line</dt>
            <dd>
              <code>{slot.evidence.line_ref}</code>
            </dd>
          </div>
        ) : null}
        {"span_ref" in slot.evidence && slot.evidence.span_ref ? (
          <div>
            <dt>Span</dt>
            <dd>
              <code>{slot.evidence.span_ref}</code>
            </dd>
          </div>
        ) : null}
        {"path" in slot.evidence && slot.evidence.path ? (
          <div>
            <dt>Path</dt>
            <dd>
              <code>{slot.evidence.path}</code>
            </dd>
          </div>
        ) : null}
      </dl>

      {slot.evidence.claim ? (
        <p className="evidence__claim">
          <span className="text-muted">Claim: </span>
          {slot.evidence.claim}
        </p>
      ) : null}

      {resolution.isPending ? <InlineLoading>Resolving evidence</InlineLoading> : null}

      {resolution.isError ? (
        <ErrorState
          error={resolution.error}
          compact
          onRetry={() => void resolution.refetch()}
        />
      ) : null}

      {resolution.data?.status === "resolved" ? (
        <div className="evidence__result evidence__result--resolved">
          <span className="badge badge--success">Resolved</span>
          <p>{resolution.data.summary}</p>
          {href ? (
            <a href={href} target="_blank" rel="noopener noreferrer">
              Open source
              <span className="visually-hidden"> (opens in a new tab)</span>
            </a>
          ) : null}
        </div>
      ) : null}

      {resolution.data?.status === "unsupported" ? (
        <div className="evidence__result evidence__result--unsupported" role="status">
          <span className="badge badge--danger">Unsupported</span>
          <p>{resolution.data.reason ?? "This citation could not be resolved."}</p>
        </div>
      ) : null}
    </article>
  );
}
