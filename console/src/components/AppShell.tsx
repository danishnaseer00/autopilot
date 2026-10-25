import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { useOnline } from "../hooks/useOnline";
import { Button } from "./Button";

const NAV = [
  { to: "/projects", label: "Projects" },
  { to: "/incidents", label: "Incidents" },
  { to: "/billing", label: "Billing" },
  { to: "/reports", label: "Reports" },
] as const;

export function AppShell() {
  const auth = useAuth();
  const navigate = useNavigate();
  const online = useOnline();

  function signOut() {
    auth.signOut();
    navigate("/login", { replace: true });
  }

  function changeOrg(slug: string) {
    auth.setActiveOrg(slug);
    navigate("/projects");
  }

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>

      {!online ? (
        <div className="offline-banner" role="alert">
          You are offline. The console will retry when the connection returns. Nothing on the
          server was changed by this tab.
        </div>
      ) : null}

      <aside className="sidebar">
        <div className="sidebar__brand">
          <span className="brand__mark" aria-hidden="true">
            AP
          </span>
          <div>
            <p className="brand">AutoPilot</p>
            <p className="sidebar__brand-sub">Platform console</p>
          </div>
        </div>

        {auth.activeOrg ? (
          <div className="org-switcher">
            <label className="visually-hidden" htmlFor="active-org">
              Active organisation
            </label>
            {auth.orgs.length > 1 ? (
              <select
                id="active-org"
                className="select select--compact"
                value={auth.activeOrg.slug}
                onChange={(event) => changeOrg(event.target.value)}
              >
                {auth.orgs.map((org) => (
                  <option key={org.slug} value={org.slug}>
                    {org.name}
                  </option>
                ))}
              </select>
            ) : (
              <p className="org-switcher__name">{auth.activeOrg.name}</p>
            )}
            <p className="org-switcher__role">
              <span className="visually-hidden">Your role: </span>
              {auth.activeOrg.role}
            </p>
          </div>
        ) : null}

        <nav className="sidebar__nav" aria-label="Primary">
          <ul>
            {NAV.map((item) => (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  className={({ isActive }) => `nav-link${isActive ? " nav-link--active" : ""}`}
                >
                  {item.label}
                </NavLink>
              </li>
            ))}
            {auth.isPlatformAdmin ? (
              <li>
                <NavLink
                  to="/admin"
                  className={({ isActive }) => `nav-link${isActive ? " nav-link--active" : ""}`}
                >
                  Admin
                </NavLink>
              </li>
            ) : null}
          </ul>
        </nav>

        <div className="sidebar__footer">
          {auth.isMock ? <span className="chip chip--demo">Demo data</span> : null}
          <p className="sidebar__user">
            <span className="sidebar__user-name">{auth.me?.user.display_name}</span>
            <span className="sidebar__user-email">{auth.me?.user.email}</span>
          </p>
          <Button size="sm" onClick={signOut}>
            {auth.canSignOut ? "Sign out" : "Leave console"}
          </Button>
          {!auth.canSignOut ? (
            <p className="sidebar__note">
              Single sign-on is not configured for this deployment; leave the console at the
              platform edge.
            </p>
          ) : null}
        </div>
      </aside>

      <main className="main" id="main-content" tabIndex={-1}>
        <Outlet />
      </main>
    </div>
  );
}
