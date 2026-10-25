import { Callout } from "../../components/Callout";
import { PageHeader } from "../../components/PageHeader";
import { useDocumentTitle } from "../../hooks/useDocumentTitle";

const PLANNED = [
  ["Organisations", "List, inspect, suspend and resume tenants (GET /admin/organizations, suspend/resume)."],
  ["Plans", "Read and edit plan limits (GET/PATCH /admin/plans)."],
  ["Capacity", "Worker and queue capacity (GET /admin/capacity)."],
  ["Aggregate reports", "Platform-level reports only; never per-org detail (GET /admin/reports/{period})."],
  ["Audit", "Filter by org, action and time range (GET /admin/audit)."],
];

export default function AdminPage() {
  useDocumentTitle("Admin");
  return (
    <div className="stack stack--loose">
      <PageHeader
        eyebrow="Platform administration"
        title="Admin"
        description="Tenant operations, plans, capacity and the audit log. Server-gated on the platform-admin flag."
      />
      <Callout tone="info" title="Labelled shell in this slice">
        The admin area is behind both the client gate and the server platform-admin check. This
        screen is a shell: it does not call administrative endpoints, so an accidental click cannot
        suspend a tenant. The full admin slice follows coordinator review.
      </Callout>
      <section className="panel" aria-labelledby="admin-planned-title">
        <h2 id="admin-planned-title">Planned surface</h2>
        <ul className="feature-list">
          {PLANNED.map(([title, description]) => (
            <li key={title}>
              <strong>{title}</strong>
              <span>{description}</span>
            </li>
          ))}
        </ul>
        <p className="text-muted">
          A non-admin request must return 403 server-side (SPEC-36 AC-7); the client gate is a
          courtesy, never the control.
        </p>
      </section>
    </div>
  );
}
