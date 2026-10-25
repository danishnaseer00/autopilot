import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/endpoints";
import { isApiError } from "../../api/errors";
import type { IncidentAction } from "../../api/types";
import { Button } from "../../components/Button";
import { Callout } from "../../components/Callout";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { LoadingPanel } from "../../components/Loading";
import { PageHeader } from "../../components/PageHeader";
import { ErrorState } from "../../components/ProblemView";
import { StatusBadge } from "../../components/StatusBadge";
import { NotFoundPage } from "../../components/StatusPages";
import { useDocumentTitle } from "../../hooks/useDocumentTitle";
import { useProject } from "../../hooks/useProject";
import {
  formatDateTime,
  formatRelative,
  humanize,
  pluralize,
} from "../../lib/format";
import { EvidenceCard } from "./EvidenceCard";
import { flattenEvidence } from "./evidence";

type Decision = "approve" | "reject";

interface PendingDecision {
  kind: Decision;
  action: IncidentAction;
}

function paramsSummary(params: Record<string, unknown>): string {
  const entries = Object.entries(params);
  if (entries.length === 0) return "No parameters";
  return entries.map(([key, value]) => `${key}=${String(value)}`).join(", ");
}

function approvalExpired(action: IncidentAction): boolean {
  if (!action.approval_expires_at) return false;
  return Date.parse(action.approval_expires_at) <= Date.now();
}

export default function IncidentDetailPage() {
  const { incidentId = "" } = useParams<{ incidentId: string }>();
  const queryClient = useQueryClient();
  useDocumentTitle("Incident");

  const incident = useQuery({
    queryKey: ["incident", incidentId],
    queryFn: ({ signal }) => api.incidents.get(incidentId, signal),
    enabled: Boolean(incidentId),
  });

  const project = useProject(incident.data?.project_id);

  const timeline = useQuery({
    queryKey: ["incident", incidentId, "timeline"],
    queryFn: ({ signal }) => api.incidents.timeline(incidentId, signal),
    enabled: Boolean(incidentId),
  });

  const [decision, setDecision] = useState<PendingDecision | null>(null);
  const [actionError, setActionError] = useState<unknown>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);

  const evidence = useMemo(
    () => (incident.data ? flattenEvidence(incident.data) : []),
    [incident.data],
  );

  const review = useMutation({
    mutationFn: ({ kind, action }: PendingDecision) =>
      kind === "approve"
        ? api.incidents.approve(incidentId, action.id)
        : api.incidents.reject(incidentId, action.id),
    onSuccess: (_updated, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["incident", incidentId] });
      setActionMessage(
        variables.kind === "approve"
          ? `Approved ${humanize(variables.action.action)} on ${variables.action.target}.`
          : `Rejected ${humanize(variables.action.action)} on ${variables.action.target}.`,
      );
      setActionError(null);
      setDecision(null);
    },
    onError: (error) => {
      setActionError(error);
      setDecision(null);
    },
  });

  if (!incidentId) return <NotFoundPage />;
  if (incident.isPending) return <LoadingPanel label="Loading incident" lines={7} />;
  if (incident.isError) {
    if (isApiError(incident.error) && incident.error.code === "not_found") return <NotFoundPage />;
    return <ErrorState error={incident.error} onRetry={() => void incident.refetch()} />;
  }

  const data = incident.data;
  const causes = [...data.causes].sort((a, b) => a.rank - b.rank);
  const pendingActions = data.actions.filter((action) => action.status === "pending_approval");
  const confidencePercent = Math.round(data.diagnosis.confidence * 100);

  return (
    <div className="stack stack--loose">
      <nav className="breadcrumbs" aria-label="Breadcrumb">
        <ol>
          <li>
            <Link to="/incidents">Incidents</Link>
          </li>
          <li aria-current="page">{data.id}</li>
        </ol>
      </nav>

      <PageHeader
        eyebrow={`Detected ${formatRelative(data.detected_at)} by ${data.detected_by}`}
        title={data.fault_signature ? humanize(data.fault_signature) : `Incident ${data.id}`}
        description={
          <div className="incident-meta">
            <StatusBadge status={data.severity} srPrefix="Severity" />
            <StatusBadge status={data.status} srPrefix="Incident status" />
            {project.data ? (
              <Link to={`/projects/${project.data.id}`}>{project.data.name}</Link>
            ) : (
              <code>{data.project_id}</code>
            )}
            {data.deployment_id ? (
              <Link to={`/projects/${data.project_id}/deployments/${data.deployment_id}`}>
                Deployment {data.deployment_id}
              </Link>
            ) : null}
          </div>
        }
      />

      {!data.recovery_verified ? (
        <Callout tone="warning" title="Recovery is not verified">
          The incident is {data.status}. A resolution only counts as recovered when the same detector
          that opened it observes healthy behaviour. Do not treat this incident as closed evidence
          until that check passes.
        </Callout>
      ) : null}

      {actionMessage ? (
        <p className="success-message" role="status" aria-live="polite">
          {actionMessage}
        </p>
      ) : null}
      {actionError ? (
        <ErrorState
          error={actionError}
          compact
          onReload={() => {
            setActionError(null);
            void incident.refetch();
            void timeline.refetch();
          }}
        />
      ) : null}

      <section className="panel" aria-labelledby="diagnosis-title">
        <div className="panel__header">
          <div>
            <h2 id="diagnosis-title">Diagnosis</h2>
            <p className="panel__subtitle">
              {pluralize(data.diagnosis.claims.length, "claim")} ·{" "}
              {pluralize(evidence.length, "evidence citation")}
            </p>
          </div>
          <div className="confidence">
            <progress value={confidencePercent} max={100} aria-label="Diagnosis confidence">
              {confidencePercent}%
            </progress>
            <span>{confidencePercent}% confidence</span>
          </div>
        </div>
        <p className="diagnosis-summary">{data.diagnosis.summary}</p>
        <dl className="definition-list definition-list--compact">
          {data.diagnosis.model_version ? (
            <div>
              <dt>Model</dt>
              <dd>
                <code>{data.diagnosis.model_version}</code>
              </dd>
            </div>
          ) : null}
          {data.diagnosis.corpus_version ? (
            <div>
              <dt>Corpus</dt>
              <dd>
                <code>{data.diagnosis.corpus_version}</code>
              </dd>
            </div>
          ) : null}
          {data.diagnosis.runbook_refs?.length ? (
            <div>
              <dt>Runbooks</dt>
              <dd>{data.diagnosis.runbook_refs.join(", ")}</dd>
            </div>
          ) : null}
          {data.diagnosis.similar_incidents?.length ? (
            <div>
              <dt>Similar</dt>
              <dd>{data.diagnosis.similar_incidents.join(", ")}</dd>
            </div>
          ) : null}
        </dl>
      </section>

      <section className="panel" aria-labelledby="causes-title">
        <div className="panel__header">
          <div>
            <h2 id="causes-title">Ranked causes</h2>
            <p className="panel__subtitle">
              Every citation is resolved inline. Unsupported results stay visible.
            </p>
          </div>
        </div>
        {causes.length === 0 ? (
          <p className="text-muted">No ranked causes were retained for this incident.</p>
        ) : (
          <ol className="cause-list">
            {causes.map((cause) => (
              <li key={`${cause.rank}-${cause.component}`} className="cause">
                <div className="cause__head">
                  <span className="cause__rank" aria-hidden="true">
                    #{cause.rank}
                  </span>
                  <h3>{cause.component}</h3>
                  <span className="cause__score">{Math.round(cause.score * 100)}% score</span>
                </div>
                <div className="evidence-grid">
                  {evidence
                    .filter((slot) => slot.source === "ranked_cause" && slot.causeRank === cause.rank)
                    .map((slot) => (
                      <EvidenceCard key={slot.n} incidentId={data.id} slot={slot} />
                    ))}
                </div>
              </li>
            ))}
          </ol>
        )}
      </section>

      <section className="panel" aria-labelledby="claims-title">
        <div className="panel__header">
          <div>
            <h2 id="claims-title">Falsifiable claims</h2>
            <p className="panel__subtitle">
              Each claim carries structured citations, not free text (SPEC-36 R-3).
            </p>
          </div>
        </div>
        {data.diagnosis.claims.length === 0 ? (
          <p className="text-muted">No diagnosis claims were retained.</p>
        ) : (
          <ol className="claim-list">
            {data.diagnosis.claims.map((claim, claimIndex) => (
              <li key={`${claimIndex}-${claim.claim}`} className="claim">
                <h3>{claim.claim}</h3>
                <div className="evidence-grid">
                  {evidence
                    .filter(
                      (slot) =>
                        slot.source === "diagnosis_claim" && slot.claim === claim.claim,
                    )
                    .map((slot) => (
                      <EvidenceCard key={slot.n} incidentId={data.id} slot={slot} />
                    ))}
                </div>
              </li>
            ))}
          </ol>
        )}
      </section>

      <section className="panel" aria-labelledby="approvals-title">
        <div className="panel__header">
          <div>
            <h2 id="approvals-title">Approvals</h2>
            <p className="panel__subtitle">
              Higher-risk actions wait here. Approval is recorded against your account.
            </p>
          </div>
        </div>

        {pendingActions.length === 0 ? (
          <p className="text-muted">No actions are awaiting approval.</p>
        ) : (
          <ul className="approval-list">
            {pendingActions.map((action) => {
              const expired = approvalExpired(action);
              return (
                <li key={action.id} className="approval">
                  <div>
                    <h3>
                      {humanize(action.action)} on <code>{action.target}</code>
                    </h3>
                    <p className="text-muted">{paramsSummary(action.params)}</p>
                    <p>
                      Approval window ends{" "}
                      {action.approval_expires_at
                        ? formatDateTime(action.approval_expires_at)
                        : "when the incident closes"}
                      .
                    </p>
                    {expired ? (
                      <p className="field__error" role="status">
                        The approval window has expired. The action cannot be approved.
                      </p>
                    ) : null}
                  </div>
                  <div className="row-actions">
                    <Button
                      variant="primary"
                      onClick={() => {
                        setActionError(null);
                        setDecision({ kind: "approve", action });
                      }}
                      disabled={expired}
                    >
                      Approve
                      <span className="visually-hidden">
                        {" "}
                        {humanize(action.action)} on {action.target}
                      </span>
                    </Button>
                    <Button
                      variant="danger"
                      onClick={() => {
                        setActionError(null);
                        setDecision({ kind: "reject", action });
                      }}
                    >
                      Reject
                      <span className="visually-hidden">
                        {" "}
                        {humanize(action.action)} on {action.target}
                      </span>
                    </Button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}

        <h3 className="section-subtitle">All actions</h3>
        <div className="table-wrap">
          <table className="data-table data-table--compact">
            <caption className="visually-hidden">All actions proposed for this incident</caption>
            <thead>
              <tr>
                <th scope="col">Action</th>
                <th scope="col">Target</th>
                <th scope="col">Status</th>
                <th scope="col">Approval</th>
                <th scope="col">Executed</th>
              </tr>
            </thead>
            <tbody>
              {data.actions.map((action) => (
                <tr key={action.id}>
                  <td>{humanize(action.action)}</td>
                  <td>
                    <code>{action.target}</code>
                  </td>
                  <td>
                    <StatusBadge status={action.status} srPrefix="Action status" />
                  </td>
                  <td>
                    {action.requires_approval ? "Required" : "Not required"}
                    {action.approved_at ? (
                      <span className="table-secondary">
                        {formatDateTime(action.approved_at)}
                      </span>
                    ) : null}
                  </td>
                  <td>{action.executed_at ? formatDateTime(action.executed_at) : "Not executed"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel" aria-labelledby="timeline-title">
        <div className="panel__header">
          <div>
            <h2 id="timeline-title">Timeline</h2>
            <p className="panel__subtitle">Ordered events, including agent actions and approvals.</p>
          </div>
        </div>
        {timeline.isPending ? <LoadingPanel label="Loading timeline" lines={3} /> : null}
        {timeline.isError ? (
          <ErrorState error={timeline.error} onRetry={() => void timeline.refetch()} />
        ) : null}
        {timeline.data?.length === 0 ? (
          <p className="text-muted">No timeline events were returned.</p>
        ) : null}
        {timeline.data && timeline.data.length > 0 ? (
          <ol className="timeline">
            {[...timeline.data]
              .sort((a, b) => a.at.localeCompare(b.at))
              .map((event) => (
                <li key={event.id} className="timeline__event">
                  <div className="timeline__marker" aria-hidden="true" />
                  <div>
                    <p className="timeline__summary">{event.summary}</p>
                    <p className="timeline__meta">
                      <time dateTime={event.at}>{formatDateTime(event.at)}</time>
                      {" · "}
                      <code>{event.actor}</code>
                      {" · "}
                      <span className="badge badge--neutral">{humanize(event.kind)}</span>
                    </p>
                  </div>
                </li>
              ))}
          </ol>
        ) : null}
      </section>

      <ConfirmDialog
        open={decision !== null}
        title={
          decision?.kind === "approve"
            ? `Approve ${humanize(decision.action.action)}?`
            : `Reject ${decision ? humanize(decision.action.action) : "this action"}?`
        }
        description={
          decision ? (
            <p>
              {humanize(decision.action.action)} on <code>{decision.action.target}</code>.{" "}
              {paramsSummary(decision.action.params)}.
            </p>
          ) : null
        }
        consequence={
          decision?.kind === "approve"
            ? "The AutoPilot agent will execute this action immediately. You are recorded as the approver, and the action may change running production services."
            : "The agent will not execute this action. The incident may remain unresolved and require manual recovery. This decision is recorded and cannot be undone."
        }
        confirmLabel={decision?.kind === "approve" ? "Approve action" : "Reject action"}
        tone={decision?.kind === "approve" ? "primary" : "danger"}
        busy={review.isPending}
        onCancel={() => setDecision(null)}
        onConfirm={() => {
          if (decision) review.mutate(decision);
        }}
      />
    </div>
  );
}
