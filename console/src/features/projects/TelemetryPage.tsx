import { useParams } from "react-router-dom";
import { Callout } from "../../components/Callout";
import { PageHeader } from "../../components/PageHeader";
import { useDocumentTitle } from "../../hooks/useDocumentTitle";

export default function TelemetryPage() {
  const { projectId } = useParams<{ projectId: string }>();
  useDocumentTitle("Telemetry");

  return (
    <div className="stack">
      <PageHeader
        title="Telemetry"
        description="Project-level metrics and logs as an embedded observability surface."
      />
      <Callout tone="info" title="Shell in this slice">
        SPEC-36 Q-2 defaults to embedding Grafana panels for incident telemetry, but the contract
        exposes no telemetry or dashboard-URL endpoint for project {projectId}. Building a chart
        from invented metrics would violate the contract rule, so this tab is intentionally a
        labelled shell.
      </Callout>
      <section className="panel panel--muted">
        <h2>What exists instead</h2>
        <p>
          Build logs are live on each deployment detail through{" "}
          <code>GET /deployments/&#123;id&#125;/logs?from=</code>. Incident evidence resolves
          through the evidence endpoint, not a dashboard.
        </p>
      </section>
    </div>
  );
}
