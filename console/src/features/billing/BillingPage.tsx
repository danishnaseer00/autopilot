import { Callout } from "../../components/Callout";
import { PageHeader } from "../../components/PageHeader";
import { useDocumentTitle } from "../../hooks/useDocumentTitle";

const PLANNED = [
  ["Plan and usage", "Plan limits, current usage vs each limit, reset times (GET /orgs/{slug}/usage, GET /plans)."],
  ["Checkout", "Plan selection and a redirect to the billing provider (POST /billing/checkout)."],
  ["Customer portal", "Payment method, invoices and cancellation via the provider portal (POST /billing/portal)."],
  ["Subscription state", "Status, period end and cancel-at-period-end (GET /billing/subscription, cancel/resume)."],
];

export default function BillingPage() {
  useDocumentTitle("Billing");
  return (
    <div className="stack stack--loose">
      <PageHeader
        eyebrow="Commercial"
        title="Billing"
        description="Plan, usage against limits, checkout and invoices."
      />
      <Callout tone="info" title="Labelled shell in this slice">
        The first console slice stops at projects, deployments, incidents and their error states.
        Billing is intentionally not implemented against the API yet, so no checkout or
        cancellation can be triggered from here. This is a scope decision, not a missing screen.
      </Callout>
      <section className="panel" aria-labelledby="billing-planned-title">
        <h2 id="billing-planned-title">What the billing slice will contain</h2>
        <ul className="feature-list">
          {PLANNED.map(([title, description]) => (
            <li key={title}>
              <strong>{title}</strong>
              <span>{description}</span>
            </li>
          ))}
        </ul>
        <p className="text-muted">
          Money is integer minor units with an explicit currency (SPEC-36 §2). The console will
          never compute a total from a float.
        </p>
      </section>
    </div>
  );
}
