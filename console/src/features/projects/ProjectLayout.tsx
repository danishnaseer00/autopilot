import { Link, NavLink, Outlet, useParams } from "react-router-dom";
import { isApiError } from "../../api/errors";
import { useProject } from "../../hooks/useProject";
import { PageHeader } from "../../components/PageHeader";
import { LoadingPanel } from "../../components/Loading";
import { ErrorState } from "../../components/ProblemView";
import { StatusBadge } from "../../components/StatusBadge";
import { NotFoundPage } from "../../components/StatusPages";
import { useDocumentTitle } from "../../hooks/useDocumentTitle";

export default function ProjectLayout() {
  const { projectId } = useParams<{ projectId: string }>();
  const project = useProject(projectId);
  useDocumentTitle(project.data?.name ?? "Project");

  if (!projectId) return <NotFoundPage />;
  if (project.isPending) return <LoadingPanel label="Loading project" lines={6} />;
  if (project.isError) {
    if (isApiError(project.error) && project.error.code === "not_found") return <NotFoundPage />;
    return <ErrorState error={project.error} onRetry={() => void project.refetch()} />;
  }

  const data = project.data;
  const tabs = [
    { to: `/projects/${data.id}`, label: "Overview", end: true },
    { to: `/projects/${data.id}/deployments`, label: "Deployments" },
    { to: `/projects/${data.id}/settings`, label: "Settings" },
    { to: `/projects/${data.id}/secrets`, label: "Secrets" },
    { to: `/projects/${data.id}/domains`, label: "Domains" },
    { to: `/projects/${data.id}/telemetry`, label: "Telemetry" },
  ];

  return (
    <>
      <nav className="breadcrumbs" aria-label="Breadcrumb">
        <ol>
          <li>
            <Link to="/projects">Projects</Link>
          </li>
          <li aria-current="page">{data.name}</li>
        </ol>
      </nav>

      <PageHeader
        title={data.name}
        description={
          <div className="project-meta">
            <StatusBadge status={data.status} srPrefix="Project status" />
            <span className="mono">{data.connected_repo?.full_name ?? "No repository connected"}</span>
            <span>Default branch: <code>{data.default_branch}</code></span>
            <span className="badge-row">
              {data.environments.map((environment) => (
                <StatusBadge key={environment} status={environment} />
              ))}
            </span>
          </div>
        }
      />

      <nav className="tabs" aria-label="Project sections">
        <ul>
          {tabs.map((tab) => (
            <li key={tab.to}>
              <NavLink
                to={tab.to}
                end={tab.end}
                className={({ isActive }) => `tabs__link${isActive ? " tabs__link--active" : ""}`}
              >
                {tab.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>

      <Outlet />
    </>
  );
}
