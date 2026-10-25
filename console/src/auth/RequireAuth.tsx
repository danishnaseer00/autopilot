import { Link, Navigate, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "./AuthContext";
import { ErrorState } from "../components/ProblemView";
import { FullPageLoading } from "../components/Loading";
import { buttonClass } from "../components/Button";
import { useDocumentTitle } from "../hooks/useDocumentTitle";

/**
 * SPEC-37 R-6: `unauthenticated` redirects to login and preserves the intended
 * route. The login page only trusts same-origin absolute paths.
 */
export function RequireAuth() {
  const auth = useAuth();
  const location = useLocation();
  useDocumentTitle(auth.status === "authenticated" ? "Console" : "Sign in");

  if (auth.status === "loading") return <FullPageLoading />;

  if (auth.status === "anonymous") {
    const next = encodeURIComponent(`${location.pathname}${location.search}`);
    return <Navigate to={`/login?next=${next}`} replace />;
  }

  if (auth.status === "suspended") {
    return (
      <div className="full-page-center">
        <div className="status-page">
          <p className="eyebrow">Organisation suspended</p>
          <h1>This organisation is suspended</h1>
          <p>
            Access is blocked for every member of the organisation. Leave the console and contact
            your platform administrator to resolve the suspension.
          </p>
          <button type="button" className={buttonClass("primary")} onClick={auth.signOut}>
            {auth.canSignOut ? "Sign out" : "Leave console"}
          </button>
        </div>
      </div>
    );
  }

  if (auth.status === "error") {
    return (
      <div className="full-page-center">
        <div className="status-page status-page--wide">
          <p className="eyebrow">Session check failed</p>
          <h1>The console could not load your session</h1>
          <ErrorState error={auth.error} onRetry={() => void auth.reloadSession()} />
          <Link className={buttonClass("secondary")} to="/login">
            Go to sign in
          </Link>
        </div>
      </div>
    );
  }

  return <Outlet />;
}

/** SPEC-36 §3.8: the server is authoritative; this is the client-side courtesy gate. */
export function RequireAdmin() {
  const auth = useAuth();
  if (!auth.isPlatformAdmin) return <ForbiddenPage />;
  return <Outlet />;
}

export function ForbiddenPage() {
  useDocumentTitle("Access denied");
  return (
    <div className="status-page">
      <p className="eyebrow">403 · Forbidden</p>
      <h1>You do not have access to this area</h1>
      <p>
        Platform administration is limited to users with the platform administrator role. Your
        organisation role does not grant it. Retrying will not change the answer; ask a platform
        administrator if you need access.
      </p>
      <Link className={buttonClass("primary")} to="/projects">
        Return to projects
      </Link>
    </div>
  );
}
