import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { FullPageLoading } from "./components/Loading";
import { NotFoundPage } from "./components/StatusPages";
import { RequireAdmin, RequireAuth } from "./auth/RequireAuth";

const LoginPage = lazy(() => import("./auth/LoginPage"));
const ProjectsListPage = lazy(() => import("./features/projects/ProjectsListPage"));
const OnboardingPage = lazy(() => import("./features/projects/OnboardingPage"));
const NewProjectPage = lazy(() => import("./features/projects/NewProjectPage"));
const ProjectLayout = lazy(() => import("./features/projects/ProjectLayout"));
const ProjectOverviewPage = lazy(() => import("./features/projects/ProjectOverviewPage"));
const ProjectSettingsPage = lazy(() => import("./features/projects/ProjectSettingsPage"));
const SecretsPage = lazy(() => import("./features/projects/SecretsPage"));
const DomainsPage = lazy(() => import("./features/projects/DomainsPage"));
const TelemetryPage = lazy(() => import("./features/projects/TelemetryPage"));
const DeploymentsListPage = lazy(() => import("./features/deployments/DeploymentsListPage"));
const DeploymentDetailPage = lazy(() => import("./features/deployments/DeploymentDetailPage"));
const IncidentsListPage = lazy(() => import("./features/incidents/IncidentsListPage"));
const IncidentDetailPage = lazy(() => import("./features/incidents/IncidentDetailPage"));
const BillingPage = lazy(() => import("./features/billing/BillingPage"));
const ReportsPage = lazy(() => import("./features/reports/ReportsPage"));
const AdminPage = lazy(() => import("./features/admin/AdminPage"));

export function AppRoutes() {
  return (
    <Suspense fallback={<FullPageLoading />}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />

        <Route element={<RequireAuth />}>
          <Route element={<AppShell />}>
            <Route index element={<Navigate to="/projects" replace />} />
            <Route path="projects" element={<ProjectsListPage />} />
            <Route path="onboarding" element={<OnboardingPage />} />
            <Route path="projects/new" element={<NewProjectPage />} />
            <Route path="projects/:projectId" element={<ProjectLayout />}>
              <Route index element={<ProjectOverviewPage />} />
              <Route path="deployments" element={<DeploymentsListPage />} />
              <Route path="deployments/:deploymentId" element={<DeploymentDetailPage />} />
              <Route path="settings" element={<ProjectSettingsPage />} />
              <Route path="secrets" element={<SecretsPage />} />
              <Route path="domains" element={<DomainsPage />} />
              <Route path="telemetry" element={<TelemetryPage />} />
            </Route>
            <Route path="incidents" element={<IncidentsListPage />} />
            <Route path="incidents/:incidentId" element={<IncidentDetailPage />} />
            <Route path="billing" element={<BillingPage />} />
            <Route path="reports" element={<ReportsPage />} />
            <Route path="admin" element={<RequireAdmin />}>
              <Route index element={<AdminPage />} />
            </Route>
            <Route path="*" element={<NotFoundPage />} />
          </Route>
        </Route>
      </Routes>
    </Suspense>
  );
}
