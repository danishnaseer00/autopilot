import { Callout } from "../../components/Callout";
import { PageHeader } from "../../components/PageHeader";
import { useDocumentTitle } from "../../hooks/useDocumentTitle";

const PERIODS = ["daily", "weekly", "monthly", "yearly"];

export default function ReportsPage() {
  useDocumentTitle("Reports");
  return (
    <div className="stack stack--loose">
      <PageHeader
        eyebrow="Operations"
        title="Reports"
        description="Deployment, usage and reliability reports with PDF export."
      />
      <Callout tone="info" title="Labelled shell in this slice">
        Report charts and PDF export are deferred until the reporting slice is reviewed. The
        endpoints exist in SPEC-36 §3.6 (GET /reports/&#123;period&#125; and
        /reports/&#123;period&#125;/pdf), but this screen does not call them yet.
      </Callout>
      <section className="panel" aria-labelledby="report-periods-title">
        <h2 id="report-periods-title">Periods</h2>
        <div className="segmented" role="group" aria-label="Report period (not yet active)">
          {PERIODS.map((period) => (
            <button key={period} type="button" className="segmented__item" disabled>
              {period}
            </button>
          ))}
        </div>
        <p className="text-muted">
          The period selector is disabled rather than pretending to load a report that is not
          implemented.
        </p>
      </section>
    </div>
  );
}
