import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/endpoints";
import { useAuth } from "../../auth/AuthContext";
import { Callout } from "../../components/Callout";
import { buttonClass } from "../../components/Button";
import { PageHeader } from "../../components/PageHeader";
import { useDocumentTitle } from "../../hooks/useDocumentTitle";

export default function OnboardingPage() {
  const auth = useAuth();
  const org = auth.activeOrg?.slug ?? "";
  useDocumentTitle("Onboarding");

  const projects = useQuery({
    queryKey: ["projects", org, "onboarding"],
    queryFn: ({ signal }) => api.projects.list({ org, limit: 1 }, signal),
    enabled: Boolean(org),
  });

  const hasProject = (projects.data?.items.length ?? 0) > 0;

  return (
    <>
      <PageHeader
        eyebrow="Five-minute setup"
        title="From repository to first deploy"
        description="Three steps. A local fixture project can deploy without GitHub OAuth; connecting a real repository is the remaining setup step."
      />

      <ol className="steps">
        <li className="step">
          <span className="step__number" aria-hidden="true">
            1
          </span>
          <div className="step__body">
            <h2>Create a project</h2>
            <p>
              A project binds a name, a repository root and branch-to-environment rules. The console
              calls <code>POST /projects</code>.
            </p>
            {hasProject ? (
              <p className="step__done">At least one project exists.</p>
            ) : (
              <Link className={buttonClass("primary")} to="/projects/new">
                Create a project
              </Link>
            )}
          </div>
        </li>

        <li className="step">
          <span className="step__number" aria-hidden="true">
            2
          </span>
          <div className="step__body">
            <h2>Connect a repository (optional for local fixtures)</h2>
            <Callout tone="info" title="Local fixtures work now">
              Projects created locally use a local fixture repository, so deployment is not blocked
              on GitHub. GitHub OAuth, repository selection and webhook delivery are not implemented
              yet; a connected repository will need the commit SHA supplied by its webhook.
            </Callout>
          </div>
        </li>

        <li className="step">
          <span className="step__number" aria-hidden="true">
            3
          </span>
          <div className="step__body">
            <h2>Deploy, then watch the log</h2>
            <p>
              Open the project and trigger a deployment from its Deployments page. The local build
              worker detects the fixture, builds it, and the runtime worker applies it. The log view
              reads <code>/deployments/&#123;id&#125;/logs?from=</code>.
            </p>
            <Link className={buttonClass("secondary")} to="/projects">
              Open projects
            </Link>
          </div>
        </li>
      </ol>

      <div className="panel panel--muted">
        <h2>What you can explore with demo data</h2>
        <p>
          The local development organisation includes a Demo project and a seeded incident with
          ranked causes, resolved and unsupported evidence, and an approval action. Open a project
          to inspect deployments or the incident screen to see evidence resolution.
        </p>
        <div className="row-actions">
          <Link className={buttonClass("primary")} to="/projects">
            Go to projects
          </Link>
          <Link className={buttonClass("secondary")} to="/incidents">
            Go to incidents
          </Link>
        </div>
      </div>
    </>
  );
}
