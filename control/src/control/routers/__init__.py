"""API routers, grouped by contract area."""

from control.routers import auth, deployments, domains, incidents, orgs, projects, secrets

__all__ = ["auth", "deployments", "domains", "incidents", "orgs", "projects", "secrets"]
