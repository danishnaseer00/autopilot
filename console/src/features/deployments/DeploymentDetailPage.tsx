import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/endpoints";
import { isApiError } from "../../api/errors";
import { Button, buttonClass } from "../../components/Button";
import { Callout } from "../../components/Callout";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { LoadingPanel, InlineLoading } from "../../components/Loading";
import { ErrorState } from "../../components/ProblemView";
import { StatusBadge } from "../../components/StatusBadge";
import { NotFoundPage } from "../../components/StatusPages";
import { useDeploymentLogStream } from "../../hooks/useDeploymentLogStream";
import { useDocumentTitle } from "../../hooks/useDocumentTitle";
import { useProject } from "../../hooks/useProject";
import {
  formatDateTime,
  formatDuration,
  formatRelative,
  pluralize,
} from "../../lib/format";

function streamLabel(state: "connecting" | "open" | "reconnecting" | "closed"): string {
  if (state === "open") return "Live";
  if (state === "connecting") return "Connecting";
  if (state === "reconnecting") return "Reconnecting from the last sequence";
  return "Closed";
}

export default function DeploymentDetailPage() {
  const { projectId = "", deploymentId = "" } = useParams<{
    projectId: string;
    deploymentId: string;
  }>();
  const queryClient = useQueryClient();
  const project = useProject(projectId);
  useDocumentTitle("Deployment");

  const deployment = useQuery({
    queryKey: ["deployment", deploymentId],
    queryFn: ({ signal }) => api.deployments.get(deploymentId, signal),
    enabled: Boolean(deploymentId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "queued" || status === "building" ? 3000 : false;
    },
  });

  const graph = useQuery({
    queryKey: ["deployment", deploymentId, "graph"],
    queryFn: ({ signal }) => api.deployments.graph(deploymentId, signal),
    enabled: Boolean(deployment.data),
    retry: false,
  });

  const logs = useDeploymentLogStream(deploymentId, { enabled: Boolean(deploymentId) });
  const logRef = useRef<HTMLDivElement>(null);
  const [follow, setFollow] = useState(true);
  const [confirm, setConfirm] = useState<"promote" | "cancel" | null>(null);
  const [actionError, setActionError] = useState<unknown>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!follow || !logRef.current) return;
    logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [logs.lines, follow]);

  const cancel = useMutation({
    mutationFn: () => api.deployments.cancel(deploymentId),
    onSuccess: (result) => {
      setActionError(null);
      setActionMessage(
        result.cancelled
          ? "Build cancelled."
          : result.requested
            ? "Cancellation requested. The worker will stop after the current step."
            : "The build had already finished; nothing was cancelled.",
      );
      setConfirm(null);
      void queryClient.invalidateQueries({ queryKey: ["deployment", deploymentId] });
      void queryClient.invalidateQueries({ queryKey: ["deployments", projectId] });
      void queryClient.invalidateQueries({ queryKey: ["project", projectId] });
    },
    onError: (error) => {
      setActionError(error);
      setConfirm(null);
    },
  });

  const promote = useMutation({
    mutationFn: (revision: string) =>
      api.deployments.promote(deploymentId, { revision }),
    onSuccess: (updated) => {
      setActionError(null);
      setActionMessage(
        updated.status === "live"
          ? `Revision ${updated.revision ?? ""} is now live.`
          : "Promotion accepted.",
      );
      setConfirm(null);
      queryClient.setQueryData(["deployment", deploymentId], updated);
      void queryClient.invalidateQueries({ queryKey: ["deployments", projectId] });
      void queryClient.invalidateQueries({ queryKey: ["project", projectId] });
    },
    onError: (error) => {
      setActionError(error);
      setConfirm(null);
    },
  });

  if (!deploymentId) return <NotFoundPage />;
  if (deployment.isPending) return <LoadingPanel label="Loading deployment" lines={6} />;
  if (deployment.isError) {
    if (isApiError(deployment.error) && deployment.error.code === "not_found") {
      return <NotFoundPage />;
    }
    return <ErrorState error={deployment.error} onRetry={() => void deployment.refetch()} />;
  }

  const data = deployment.data;
  const canPromote =
    Boolean(data.revision) &&
    data.status !== "live" &&
    ["ready", "superseded", "cancelled"].includes(data.status);
  const canCancel = data.status === "queued" || data.status === "building";
  const isRollback = data.status === "superseded";

  return (
    <div className="stack stack--loose">
      <nav className="breadcrumbs" aria-label="Breadcrumb">
        <ol>
          <li>
            <Link to="/projects">Projects</Link>
          </li>
          <li>
            <Link to={`/projects/${projectId}`}>{project.data?.name ?? projectId}</Link>
          </li>
          <li>
            <Link to={`/projects/${projectId}/deployments`}>Deployments</Link>
          </li>
          <li aria-current="page">{data.git_sha.slice(0, 7)}</li>
        </ol>
      </nav>

      <header className="page-header">
        <div className="page-header__text">
          <p className="eyebrow">Deployment</p>
          <h1>
            {data.git_sha.slice(0, 7)} on <code>{data.git_ref}</code>
          </h1>
          <div className="page-header__description">
            <div className="badge-row">
              <StatusBadge status={data.status} srPrefix="Deployment status" />
              <StatusBadge status={data.environment} />
              <span className="text-muted">Triggered by {data.trigger_source}</span>
            </div>
          </div>
        </div>
        <div className="page-header__actions">
          {canCancel ? (
            <Button variant="danger" onClick={() => setConfirm("cancel")}>
              Cancel build
            </Button>
          ) : null}
          {canPromote ? (
            <Button variant="primary" onClick={() => setConfirm("promote")}>
              {isRollback ? "Roll back to this revision" : "Promote revision"}
            </Button>
          ) : null}
          <Link className={buttonClass("secondary")} to={`/projects/${projectId}/deployments`}>
            Back to deployments
          </Link>
        </div>
      </header>

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
            void deployment.refetch();
          }}
        />
      ) : null}

      {isRollback ? (
        <Callout tone="info" title="Rollback uses the promote endpoint">
          SPEC-36 §3.3 has no rollback endpoint (GAP-G13). A retained revision is restored by
          promoting this deployment, which is the same state transition the API exposes.
        </Callout>
      ) : null}

      <section className="stat-grid" aria-label="Deployment summary">
        <div className="stat">
          <p className="stat__label">Stage</p>
          <p className="stat__value stat__value--small">{data.stage ?? "Not reported"}</p>
        </div>
        <div className="stat">
          <p className="stat__label">Started</p>
          <p className="stat__value stat__value--small">{formatDateTime(data.started_at)}</p>
        </div>
        <div className="stat">
          <p className="stat__label">Duration</p>
          <p className="stat__value stat__value--small">
            {formatDuration(data.started_at, data.finished_at)}
          </p>
        </div>
        <div className="stat">
          <p className="stat__label">Exit code</p>
          <p className="stat__value stat__value--small">
            {data.exit_code === null ? "Not reported" : data.exit_code}
          </p>
        </div>
      </section>

      <div className="split-grid">
        <section className="panel" aria-labelledby="revision-title">
          <h2 id="revision-title">Revision</h2>
          <dl className="definition-list">
            <div>
              <dt>Revision</dt>
              <dd>
                <code>{data.revision ?? "Not promoted"}</code>
              </dd>
            </div>
            <div>
              <dt>Manifest</dt>
              <dd>
                <code>{data.manifest_hash ?? "Not reported"}</code>
              </dd>
            </div>
            <div>
              <dt>Image digest</dt>
              <dd>
                <code>{data.image_digest ?? "Not reported"}</code>
              </dd>
            </div>
            <div>
              <dt>Created</dt>
              <dd>
                <time dateTime={data.created_at}>{formatRelative(data.created_at)}</time>
              </dd>
            </div>
            <div>
              <dt>Finished</dt>
              <dd>{formatDateTime(data.finished_at)}</dd>
            </div>
            {data.preview_hostname ? (
              <div>
                <dt>Preview hostname</dt>
                <dd>
                  <code>{data.preview_hostname}</code>
                </dd>
              </div>
            ) : null}
          </dl>
          {data.error_message ? (
            <div className="build-error">
              <h3>Recorded build error</h3>
              <pre>{data.error_message}</pre>
            </div>
          ) : null}
        </section>

        <section className="panel" aria-labelledby="graph-title">
          <h2 id="graph-title">Dependency graph</h2>
          {graph.isPending ? <InlineLoading>Loading graph</InlineLoading> : null}
          {graph.isError ? (
            <p className="text-muted">The dependency graph is not available for this deployment.</p>
          ) : null}
          {graph.data ? (
            <>
              <h3>Services</h3>
              <ul className="plain-list">
                {graph.data.nodes.map((node) => (
                  <li key={node.id}>
                    <code>{node.id}</code> <span className="text-muted">{node.service}</span>
                  </li>
                ))}
              </ul>
              <h3>Dependencies</h3>
              {graph.data.edges.length === 0 ? (
                <p className="text-muted">No declared dependencies.</p>
              ) : (
                <ul className="plain-list">
                  {graph.data.edges.map((edge) => (
                    <li key={`${edge.from}-${edge.to}`}>
                      <code>{edge.from}</code> → <code>{edge.to}</code>
                    </li>
                  ))}
                </ul>
              )}
            </>
          ) : null}
        </section>
      </div>

      <section className="panel panel--logs" aria-labelledby="logs-title">
        <div className="panel__header panel__header--wrap">
          <div>
            <h2 id="logs-title">Build log</h2>
            <p className="panel__subtitle">
              SSE with sequence resume. {pluralize(logs.lines.length, "line")} in the buffer.
            </p>
          </div>
          <div className="log-toolbar">
            <p className={`stream-state stream-state--${logs.streamState}`} role="status" aria-live="polite">
              {streamLabel(logs.streamState)}
            </p>
            <label className="checkbox-inline">
              <input
                type="checkbox"
                checked={follow}
                onChange={(event) => setFollow(event.target.checked)}
              />
              <span>Follow new lines</span>
            </label>
          </div>
        </div>

        {logs.truncated ? (
          <Callout tone="warning" title="Log buffer truncated">
            Showing the most recent 5,000 lines. Earlier lines were dropped to keep browser memory
            bounded (SPEC-37 R-11).
          </Callout>
        ) : null}
        {logs.gap ? (
          <Callout tone="warning" title="Sequence gap detected">
            The stream resumed beyond the expected next sequence. Some lines may be missing; reload
            the page to request the log from the server again.
          </Callout>
        ) : null}

        <div
          ref={logRef}
          className="log-view"
          role="log"
          aria-live="polite"
          aria-relevant="additions"
          aria-label="Build log output"
          // A scrollable region must be focusable so keyboard users can scroll it.
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
          tabIndex={0}
          onScroll={(event) => {
            const element = event.currentTarget;
            const atBottom =
              element.scrollHeight - element.scrollTop - element.clientHeight < 40;
            setFollow(atBottom);
          }}
        >
          {logs.lines.length === 0 ? (
            <p className="log-view__empty">
              {logs.streamState === "closed"
                ? "No log lines were returned."
                : "Waiting for the first log line…"}
            </p>
          ) : (
            logs.lines.map((line) => (
              <div className="log-line" key={line.seq}>
                <span className="log-line__seq" aria-hidden="true">
                  {String(line.seq).padStart(5, "0")}
                </span>
                <code>{line.line}</code>
              </div>
            ))
          )}
        </div>
      </section>

      <ConfirmDialog
        open={confirm !== null}
        title={confirm === "cancel" ? "Cancel this build?" : "Promote this revision?"}
        description={
          confirm === "cancel" ? (
            <p>The worker stops at the next safe point. Partial build artifacts are discarded.</p>
          ) : (
            <p>
              The deployment status becomes <strong>live</strong> and the currently live revision is
              superseded.
            </p>
          )
        }
        consequence={
          confirm === "cancel"
            ? "Cancels this build. The current live revision keeps serving traffic. A new deployment is required to try again."
            : `Promotes revision ${data.revision ?? "from this deployment"} immediately. Traffic moves without draining in-flight requests. This can be reversed only by promoting another retained revision.`
        }
        confirmLabel={confirm === "cancel" ? "Cancel build" : "Promote revision"}
        tone={confirm === "cancel" ? "danger" : "primary"}
        busy={cancel.isPending || promote.isPending}
        onCancel={() => setConfirm(null)}
        onConfirm={() => {
          if (confirm === "cancel") cancel.mutate();
          if (confirm === "promote" && data.revision) promote.mutate(data.revision);
        }}
      />
    </div>
  );
}
