import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/endpoints";
import type { IncidentSeverity, IncidentStatus } from "../../api/types";
import { useAuth } from "../../auth/AuthContext";
import { Button, buttonClass } from "../../components/Button";
import { EmptyState } from "../../components/EmptyState";
import { LoadingPanel } from "../../components/Loading";
import { PageHeader } from "../../components/PageHeader";
import { ErrorState } from "../../components/ProblemView";
import { StatusBadge } from "../../components/StatusBadge";
import { useDocumentTitle } from "../../hooks/useDocumentTitle";
import { formatRelative, humanize } from "../../lib/format";

const STATUSES: IncidentStatus[] = [
  "open",
  "diagnosed",
  "actioning",
  "resolved",
  "escalated",
];
const SEVERITIES: IncidentSeverity[] = ["critical", "high", "medium", "low"];

export default function IncidentsListPage() {
  const auth = useAuth();
  const org = auth.activeOrg?.slug ?? "";
  useDocumentTitle("Incidents");

  const [status, setStatus] = useState<"" | IncidentStatus>("");
  const [severity, setSeverity] = useState<"" | IncidentSeverity>("");

  const incidents = useQuery({
    queryKey: ["incidents", org, status, severity],
    queryFn: ({ signal }) =>
      api.incidents.list(
        org,
        {
          status: status || undefined,
          severity: severity || undefined,
        },
        signal,
      ),
    enabled: Boolean(org),
  });

  const filtersActive = Boolean(status || severity);

  return (
    <>
      <PageHeader
        eyebrow={auth.activeOrg?.name}
        title="Incidents"
        description="AutoPilot detections, ranked diagnoses, evidence resolution and approvals."
      />

      <section className="panel" aria-labelledby="incident-list-title">
        <div className="panel__header panel__header--wrap">
          <div>
            <h2 id="incident-list-title">Incident history</h2>
            <p className="panel__subtitle">
              A citation that cannot be resolved is shown as unsupported; it is never hidden
              (SPEC-36 R-4).
            </p>
          </div>
          <div className="filter-bar">
            <div className="field field--compact">
              <label className="field__label" htmlFor="incident-status">
                Status
              </label>
              <select
                id="incident-status"
                className="select select--compact"
                value={status}
                onChange={(event) => setStatus(event.target.value as "" | IncidentStatus)}
              >
                <option value="">All statuses</option>
                {STATUSES.map((value) => (
                  <option key={value} value={value}>
                    {humanize(value)}
                  </option>
                ))}
              </select>
            </div>
            <div className="field field--compact">
              <label className="field__label" htmlFor="incident-severity">
                Severity
              </label>
              <select
                id="incident-severity"
                className="select select--compact"
                value={severity}
                onChange={(event) => setSeverity(event.target.value as "" | IncidentSeverity)}
              >
                <option value="">All severities</option>
                {SEVERITIES.map((value) => (
                  <option key={value} value={value}>
                    {humanize(value)}
                  </option>
                ))}
              </select>
            </div>
            {filtersActive ? (
              <Button
                size="sm"
                onClick={() => {
                  setStatus("");
                  setSeverity("");
                }}
              >
                Clear filters
              </Button>
            ) : null}
          </div>
        </div>

        {incidents.isPending ? <LoadingPanel label="Loading incidents" lines={4} /> : null}
        {incidents.isError ? (
          <ErrorState error={incidents.error} onRetry={() => void incidents.refetch()} />
        ) : null}
        {incidents.data?.length === 0 ? (
          <EmptyState title={filtersActive ? "No incidents match these filters" : "No incidents recorded"}>
            <p>
              {filtersActive
                ? "Change the status or severity filter to see other incidents."
                : "AutoPilot has not opened an incident for this organisation. When it does, the diagnosis and its evidence will appear here."}
            </p>
          </EmptyState>
        ) : null}
        {incidents.data && incidents.data.length > 0 ? (
          <div className="table-wrap">
            <table className="data-table">
              <caption className="visually-hidden">Incidents in {auth.activeOrg?.name}</caption>
              <thead>
                <tr>
                  <th scope="col">Incident</th>
                  <th scope="col">Severity</th>
                  <th scope="col">Status</th>
                  <th scope="col">Detected</th>
                  <th scope="col">Project</th>
                  <th scope="col">Recovery</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {incidents.data.map((incident) => (
                  <tr key={incident.id}>
                    <td>
                      <Link className="table-primary" to={`/incidents/${incident.id}`}>
                        {incident.fault_signature ?? incident.id}
                      </Link>
                      <span className="table-secondary">{incident.id}</span>
                    </td>
                    <td>
                      <StatusBadge status={incident.severity} srPrefix="Severity" />
                    </td>
                    <td>
                      <StatusBadge status={incident.status} srPrefix="Incident status" />
                    </td>
                    <td>
                      <time dateTime={incident.detected_at}>
                        {formatRelative(incident.detected_at)}
                      </time>
                      <span className="table-secondary">by {incident.detected_by}</span>
                    </td>
                    <td>
                      <code>{incident.project_id}</code>
                    </td>
                    <td>
                      {incident.recovery_verified
                        ? "Verified"
                        : incident.resolved_at
                          ? "Not verified"
                          : "Not yet recovered"}
                    </td>
                    <td>
                      <Link
                        className={buttonClass("secondary", "sm")}
                        to={`/incidents/${incident.id}`}
                      >
                        Investigate
                        <span className="visually-hidden"> {incident.id}</span>
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>
    </>
  );
}
