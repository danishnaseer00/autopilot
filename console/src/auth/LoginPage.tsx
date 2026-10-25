import { useState } from "react";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { useAuth } from "./AuthContext";
import { Button } from "../components/Button";
import { Callout } from "../components/Callout";
import { FullPageLoading } from "../components/Loading";
import { ErrorState } from "../components/ProblemView";
import { useDocumentTitle } from "../hooks/useDocumentTitle";

function safeNext(raw: string | null): string {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//")) return "/projects";
  return raw;
}

/**
 * The session is an HttpOnly cookie (SPEC-37 R-18). Production deployments
 * hand the redirect to the platform SSO edge. Local development can use the
 * control API's dev-only login route, and mock mode has a demo sign-in.
 */
export default function LoginPage() {
  const auth = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [busy, setBusy] = useState(false);
  const next = safeNext(params.get("next"));
  useDocumentTitle("Sign in");

  if (auth.status === "loading") return <FullPageLoading label="Checking your session" />;
  if (auth.status === "authenticated") return <Navigate to={next} replace />;

  async function demoSignIn() {
    setBusy(true);
    try {
      await auth.signInDemo();
      navigate(next, { replace: true });
    } finally {
      setBusy(false);
    }
  }

  async function devSignIn() {
    setBusy(true);
    try {
      await auth.signInDev();
      navigate(next, { replace: true });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-page">
      <div className="login-page__panel">
        <p className="brand brand--large">
          <span className="brand__mark" aria-hidden="true">
            AP
          </span>
          AutoPilot
        </p>
        <p className="eyebrow">Platform console</p>
        <h1>Sign in</h1>
        <p className="login-page__lede">
          The console uses the platform session cookie. It never stores a token in the browser.
        </p>

        {auth.status === "suspended" ? (
          <div className="stack">
            <ErrorState error={auth.error} />
            <Button onClick={auth.signOut}>{auth.canSignOut ? "Sign out" : "Leave console"}</Button>
          </div>
        ) : auth.status === "error" ? (
          <div className="stack">
            <ErrorState error={auth.error} onRetry={() => void auth.reloadSession()} />
          </div>
        ) : auth.isMock ? (
          <div className="stack">
            <Callout tone="info" title="Demo workspace">
              This build runs against the typed SPEC-36 mock. Sign in as Maya Chen of Northstar Labs
              to explore projects, deployments, incidents and evidence.
            </Callout>
            <Button variant="primary" onClick={() => void demoSignIn()} busy={busy}>
              Sign in to the demo workspace
            </Button>
          </div>
        ) : auth.isDevLogin ? (
          <div className="stack">
            <Callout tone="info" title="Local developer workspace">
              This local build uses the control API&apos;s development login. It is unavailable in
              deployments, where the platform SSO edge owns authentication.
            </Callout>
            <Button variant="primary" onClick={() => void devSignIn()} busy={busy}>
              Sign in as local developer
            </Button>
          </div>
        ) : (
          <div className="stack">
            <Callout tone="warning" title="Single sign-on is not configured">
              This deployment has no interactive sign-in provider. The control API accepts GitHub
              OAuth settings, but the console does not start that flow yet; use the platform edge or
              enable the local developer login for development.
            </Callout>
            <p className="hint">
              If you reached this page from an expired session, return to the platform entry point
              to authenticate again.
            </p>
          </div>
        )}

        <p className="login-page__footer">
          <Link to="/">AutoPilot Console</Link> · <span>WCAG 2.1 AA target</span>
        </p>
      </div>
      <div className="login-page__aside" aria-hidden="true">
        <div className="login-page__grid" />
        <div className="login-page__quote">
          <p>“Deploy, diagnose, recover.”</p>
          <span>Projects · Deployments · Incidents</span>
        </div>
      </div>
    </div>
  );
}
