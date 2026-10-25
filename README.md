<div align="center">

<h1>AutoPilot</h1>

<p><strong>Deploy. Diagnose. Recover.</strong><br />
A self-hosted platform-as-a-service with an incident-diagnosis research plane.</p>

<p>
  <img alt="Status: local MVP" src="https://img.shields.io/badge/status-local%20MVP-0ea5e9?style=for-the-badge" />
  <img alt="Runtime: k3d / k3s" src="https://img.shields.io/badge/runtime-k3d%20%2F%20k3s-326CE5?style=for-the-badge&amp;logo=kubernetes&amp;logoColor=white" />
  <img alt="Cloud: AWS planned" src="https://img.shields.io/badge/cloud-AWS%20planned-FF9900?style=for-the-badge" />
  <img alt="Tests: local suite passing" src="https://img.shields.io/badge/tests-local%20suite%20passing-0f766e?style=for-the-badge" />
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-22c55e?style=for-the-badge" />
</p>

<p>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" />
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.115%2B-009688?style=flat-square&amp;logo=fastapi&amp;logoColor=white" />
  <img alt="React 18" src="https://img.shields.io/badge/React-18-61DAFB?style=flat-square&amp;logo=react&amp;logoColor=0b1220" />
  <img alt="TypeScript 5.7" src="https://img.shields.io/badge/TypeScript-5.7-3178C6?style=flat-square&amp;logo=typescript&amp;logoColor=white" />
  <img alt="Vite 5" src="https://img.shields.io/badge/Vite-5-646CFF?style=flat-square&amp;logo=vite&amp;logoColor=white" />
  <img alt="PostgreSQL 16" src="https://img.shields.io/badge/PostgreSQL-16-4169E1?style=flat-square&amp;logo=postgresql&amp;logoColor=white" />
  <img alt="Redis 7" src="https://img.shields.io/badge/Redis-7-DC382D?style=flat-square&amp;logo=redis&amp;logoColor=white" />
  <img alt="Docker" src="https://img.shields.io/badge/Docker-ready-2496ED?style=flat-square&amp;logo=docker&amp;logoColor=white" />
  <img alt="Kubernetes" src="https://img.shields.io/badge/Kubernetes-k3s-326CE5?style=flat-square&amp;logo=kubernetes&amp;logoColor=white" />
</p>

</div>

AutoPilot is a local-first platform-as-a-service that turns application source into a running, routable deployment and keeps the operational evidence needed to diagnose failures. It combines a FastAPI control plane, a React operations console, a build worker, a Kubernetes runtime worker, and an Autopilot research plane for fault injection and incident diagnosis.

The current milestone is a working **local vertical slice on k3d**. The same application code is designed to run on AWS k3s by changing configuration and manifests, not application code. Cloud deployment is intentionally deferred until a domain is available.

## Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [What works today](#what-works-today)
- [Tech stack](#tech-stack)
- [Repository layout](#repository-layout)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Testing](#testing)
- [Security model](#security-model)
- [API](#api)
- [Autopilot research plane](#autopilot-research-plane)
- [Roadmap](#roadmap)
- [License](#license)
- [Contributing](#contributing)

## Overview

AutoPilot separates **intent** from **execution**:

1. The control plane records projects, deployments, secrets, domains, and incidents in PostgreSQL.
2. The build worker claims a build job, detects the application shape, produces a digest-pinned image, and publishes it to an OCI registry.
3. The runtime worker claims the resulting deployment, renders Kubernetes objects, applies them to the cluster, waits for rollout, and only then cuts the route.
4. The Autopilot research plane generates deterministic fault-injection plans and evidence for incident-diagnosis experiments.

That split keeps retries, audit trails, and failure handling in one place instead of hiding them inside a single deployment script.

### Why AutoPilot

- **End-to-end local slice** — create a project, trigger a build, publish an image, deploy to k3d, and reach the application through an ingress route.
- **Tenant isolation in depth** — PostgreSQL row-level security is enabled and forced on tenant tables; the API process never holds the migrator credential.
- **Secrets that fail closed** — per-organisation data keys are wrapped by a master key; secret values are encrypted with AES-256-GCM and audited before they are returned.
- **Research-backed diagnosis** — the Autopilot track ships a frozen, reproducible fault catalogue and held-out partition rather than an ad-hoc demo.
- **Configuration-only portability** — local k3d and AWS k3s differ by Kubernetes endpoint, registry, ingress, and manifests, not by application code.

## Architecture

```mermaid
flowchart LR
    Browser["React console"] -->|"/api/v1"| API["FastAPI control plane"]
    API --> PG[("PostgreSQL 16<br/>RLS · jobs · audit")]
    API -.-> Redis[("Redis 7<br/>optional cache")]
    Build["Build worker"] -->|"claim build jobs"| PG
    Build -->|"publish digest-pinned image"| Registry[("OCI registry")]
    Runtime["Runtime worker"] -->|"apply manifests"| K8s["k3d / k3s"]
    Runtime -->|"cut route"| Ingress["NGINX ingress"]
    Ingress --> Apps["Project workloads"]
    Autopilot["Autopilot research plane"] -.->|"fault plans · evidence"| K8s
    Autopilot -.->|"incident evidence"| API
```

| Component | Responsibility | Entry point |
|---|---|---|
| **Control plane** | Auth, tenancy, projects, deployments, secrets, domains, incidents, OpenAPI | `control.main:app` |
| **Console** | Projects, deployments, secrets, domains, incidents, evidence views | `console/` |
| **Build plane** | Detection, build pipeline, queue, supply-chain controls, registry publication | `buildplane.local.worker` |
| **Runtime plane** | Secrets envelope, deployment lifecycle, Kubernetes rendering, ingress, routing | `runtime.local_worker` |
| **Shared contracts** | Typed application manifest and canonical hash | `shared/` |
| **Autopilot** | Fault catalogue, deterministic plan generation, test-app catalogue | `autopilot.inject.cli` |
| **Data layer** | PostgreSQL schema, RLS policies, forward-only migrations | `migrations/` |

The control plane never mutates the cluster directly. Workers claim work from PostgreSQL and report state back, so retries and audit are explicit.

## What works today

| Area | Status | Notes |
|---|---|---|
| Control plane | **Local** | Organisations, projects, deployments, secrets, domains, and incidents. OpenAPI at `/api/v1/docs`. |
| Console | **Local** | Real API integration, local developer login, project and deployment flows, secrets, domains, incidents, evidence. A typed mock transport is available for UI-only work. |
| Build plane | **Partial** | Detection, pipeline, queue, supply-chain, and registry modules are implemented. The local static publisher is proven end-to-end; Dockerfile and buildpack application shapes are not yet exercised end-to-end. |
| Runtime plane | **Local** | Digest-pinned manifest rendering, k3d rollout, and route cut-over work locally. The AWS path has not been deployed. |
| Autopilot | **Offline core** | The offline `verify`, `accounts`, and `plan` commands are validated. The live cluster adapter and fault-injection loop are pending; incidents in the console are seeded. |
| GitHub integration | **Partial** | Gitware modules exist, but OAuth, repository connect, and webhook delivery are not wired into the control plane. Manual deployment of a connected repository currently requires a `git_sha`. |
| Custom domains | **Partial** | Verification and TLS ledger logic exist. Public DNS verification, certificate issuance, and production routing have not been exercised end-to-end. |
| Billing / reports / admin | **Shell** | Routes and layouts exist; they are not production implementations. |
| AWS deployment | **Planned** | Manifests and production configuration are deferred until a domain is available. |

> **Honest status:** this is a strong local MVP and a research prototype, not a production-ready public cloud. The table above is the contract.

## Tech stack

| Layer | Technology |
|---|---|
| **API** | Python 3.11+, FastAPI, Pydantic, Uvicorn, asyncpg, psycopg |
| **Console** | React 18, TypeScript 5.7, Vite 5, TanStack Query 5, React Router 6 |
| **Data** | PostgreSQL 16, TimescaleDB (optional), Redis 7, SQL row-level security |
| **Build** | Python build worker, Docker sandbox, OCI registry, supply-chain controls |
| **Runtime** | Kubernetes, k3s, k3d (local), NGINX Ingress |
| **Security** | Opaque HttpOnly sessions, AES-256-GCM envelope encryption, separate database roles |
| **Tooling** | uv workspace, pytest, Vitest, Testing Library, ESLint, npm |

## Repository layout

This directory is the repository root when published. Paths below are relative to it.

```text
.
├── LICENSE               MIT license
├── autopilot/            Autopilot research plane (offline fault-injection core)
├── build/                Build worker: detection, pipeline, queue, supply chain, registry
├── console/              React + Vite operations console
├── control/              FastAPI control plane and REST API
├── docker-init/          PostgreSQL roles and databases bootstrap
├── migrations/           Forward-only SQL migrations and apply.py
├── runtime/              Runtime worker: secrets, lifecycle, Kubernetes, ingress, routing
├── shared/               Typed application manifest and canonical hash
├── tests/                Cross-cutting contract tests
├── docker-compose.dev.yml
├── pyproject.toml        uv workspace root
└── uv.lock
```

## Quick start

### Prerequisites

- Python **3.11+** and [uv](https://docs.astral.sh/uv/)
- Node.js **20+** and npm
- Docker Desktop (for PostgreSQL and Redis)
- Optional for the full deployment slice: k3d, kubectl, and a local OCI registry

### 1. Clone and configure

```bash
git clone https://github.com/danishnaseer00/autopilot.git
cd autopilot

cp .env.example .env
# PowerShell: Copy-Item .env.example .env
```

The example file contains development-only database passwords and local HTTP settings. Do not reuse them in a deployment.

### 2. Install dependencies

```bash
uv sync
```

### 3. Generate a local master key

The secrets service needs a 32-byte master key outside git. This command works on Windows, macOS, and Linux:

```bash
uv run python -c "import secrets, pathlib; p=pathlib.Path('.local/master.key'); p.parent.mkdir(exist_ok=True); p.write_bytes(secrets.token_bytes(32))"
```

`.local/` is ignored by git.

### 4. Start local services and apply the schema

```bash
docker compose -f docker-compose.dev.yml up -d
uv run --env-file .env python migrations/apply.py
```

The migration runner reads `DATABASE_URL` from the process environment, so `--env-file` loads it from `.env` for this command only. The API loads its own settings from `.env` without exporting the migrator credential into the request process.

The compose stack starts PostgreSQL 16 with TimescaleDB and Redis 7. It is local test infrastructure, not a deployment environment.

### 5. Run the API

```bash
uv run uvicorn control.main:app --host 0.0.0.0 --port 8000
```

- API docs: `http://localhost:8000/api/v1/docs`
- Liveness: `http://localhost:8000/healthz`
- Readiness: `http://localhost:8000/api/v1/healthz`

### 6. Run the console

In a second terminal:

```bash
cd console
npm install
npm run dev
```

Open `http://localhost:5173` and choose **Sign in as local developer**. The local login route is available only when `APP_ENV_NAME=dev` and `DEV_LOGIN_ENABLED=true`; it is refused in every other environment.

### 7. Exercise the full build → deploy slice

The API and console are enough to explore the product, but the full path needs the build and runtime workers plus a k3d cluster with a local registry.

```bash
# Build worker — terminal 3
uv run python -m buildplane.local.worker

# Runtime worker — terminal 4
uv run python -m runtime.local_worker
```

Both worker entry points read the process environment. Set the variables in the [worker environment](#worker-environment) table in each terminal before starting the worker.

For a local k3d cluster, expose the Kubernetes API with:

```bash
kubectl proxy --port=8001
```

Then point `KUBERNETES_BASE_URL` at `http://127.0.0.1:8001` for the runtime worker. The worker environment is documented in [Configuration](#configuration).

New projects use a **local fixture repository**, so the build path can be exercised without GitHub. Create a project, trigger a deployment, and wait for it to reach `live`. The route is served at `<project-slug>.apps.localtest.me` through the local ingress (typically port `8080`).

## Configuration

The control plane reads `.env` from the repository root. The values below are the important local settings; see [`.env.example`](.env.example) for the full template.

| Variable | Purpose | Local value |
|---|---|---|
| `APP_ENV_NAME` | Environment name. Local-only routes require `dev`. | `dev` |
| `APP_DATABASE_URL` | Application role DSN; RLS applies to request traffic. | `postgresql://autopilot_app:...@localhost:5432/autopilot_platform` |
| `AUDIT_DATABASE_URL` | Append-only audit role DSN. | `postgresql://autopilot_audit:...@localhost:5432/autopilot_audit` |
| `DATABASE_URL` | Migrator DSN for `migrations/apply.py`. The API process does not load it. | `postgresql://autopilot_migrator:...@localhost:5432/autopilot_platform` |
| `REDIS_URL` | Optional cache and coordination boundary. | `redis://localhost:6379/0` |
| `SECRET_KEY_BASE` | Application secret material. | Development placeholder |
| `DEV_LOGIN_ENABLED` | Enables the local developer login. Refused outside `APP_ENV_NAME=dev`. | `true` |
| `COOKIE_SECURE` | Sets the `Secure` flag on the session cookie. | `false` locally; `true` in production |
| `MASTER_KEY_FILE` | Path to the 32-byte master key for secret envelope encryption. | `.local/master.key` |
| `CONSOLE_BASE_URL` | Trusted console origin for CORS and links. | `http://localhost:5173` |
| `APPS_DOMAIN` | Wildcard domain used for application routes. | `apps.localtest.me` |

### Worker environment

| Variable | Used by | Purpose |
|---|---|---|
| `BUILD_DATABASE_URL` | Build worker | Build queue and reporting DSN. Falls back to `DATABASE_URL` locally. |
| `LOCAL_SOURCE_DIR` | Build worker | Source directory packaged by the local fixture fetcher. |
| `BUILD_PUSH_REGISTRY` | Build worker | Registry the local publisher pushes to. Default: `localhost:5001`. |
| `RUNTIME_REGISTRY` | Build + runtime | Registry host the cluster pulls from. Default: `k3d-autopilot-registry:5001`. |
| `BUILD_LOG_DIR` | Build worker | Local build log directory. Default: `.local/logs`. |
| `BUILD_ORG_IDS` | Build worker | Optional comma-separated organisation scope. |
| `RUNTIME_DATABASE_URL` | Runtime worker | Runtime queue and reporting DSN. Falls back to `DATABASE_URL` locally. |
| `KUBERNETES_BASE_URL` | Runtime worker | Kubernetes API endpoint. Local k3d via `kubectl proxy`: `http://127.0.0.1:8001`. |
| `KUBERNETES_TOKEN` / `KUBERNETES_CA_FILE` | Runtime worker | Credentials for a Kubernetes API outside the cluster. |
| `APPS_DOMAIN` | Runtime worker | Wildcard domain used when creating routes. |
| `RUNTIME_ORG_IDS` | Runtime worker | Optional comma-separated organisation scope. |

> **Development values only.** `.env.example` uses `devonly-change-me` and local HTTP settings. Production must set `APP_ENV_NAME=production`, `DEV_LOGIN_ENABLED=false`, `COOKIE_SECURE=true`, strong database passwords, a real `SECRET_KEY_BASE`, and a master key mounted outside the repository. Never commit `.env` or `.local/master.key`.

## Testing

Run the Python suite from the repository root:

```bash
uv run pytest
```

The schema-contract suite needs a live PostgreSQL instance and three exported DSNs. It skips cleanly when `DATABASE_URL` is not set.

```powershell
# PowerShell
$env:DATABASE_URL = "postgresql://autopilot_migrator:...@localhost:5432/autopilot_platform"
$env:APP_DATABASE_URL = "postgresql://autopilot_app:...@localhost:5432/autopilot_platform"
$env:AUDIT_DATABASE_URL = "postgresql://autopilot_audit:...@localhost:5432/autopilot_audit"
uv run pytest
```

```bash
# bash / zsh
export DATABASE_URL="postgresql://autopilot_migrator:...@localhost:5432/autopilot_platform"
export APP_DATABASE_URL="postgresql://autopilot_app:...@localhost:5432/autopilot_platform"
export AUDIT_DATABASE_URL="postgresql://autopilot_audit:...@localhost:5432/autopilot_audit"
uv run pytest
```

Run the console checks:

```bash
cd console
npm run typecheck
npm run lint
npm run test
npm run build
```

Current local snapshot: **893 passed, 1 skipped** (the skip is a Windows symlink-privilege case). Console typecheck, lint, production build, and browser smoke checks pass.

## Security model

- **Tenant isolation:** RLS is enabled and forced on tenant tables. Requests set `app.org_id` and `app.user_id` transaction-locally; without context, tenant tables return no rows.
- **Separate database roles:** the API uses an app role without `BYPASSRLS`, writes audit records through an append-only audit role, and never holds the migrator credential used for DDL.
- **Sessions:** tokens are opaque, only their SHA-256 digest is stored, and the browser receives an HttpOnly, SameSite=Lax cookie. `Secure` is on by default and is disabled only for local HTTP.
- **Secrets:** per-organisation data keys are wrapped by a master key; values use AES-256-GCM with an org/project/key AAD binding. Audit is written before a value is returned, and secret reads fail closed if audit is unavailable.
- **Build isolation:** build-visible secrets are opt-in, fork-triggered builds structurally receive none, and the build plane includes digest pinning, image scanning gates, denylist checks, install-script exceptions, egress rules, and provenance modules.
- **Repository hygiene:** `.env`, `.local/`, keys, virtual environments, build output, and `node_modules` are ignored.

These controls are covered by the local isolation and security test suites. They are not a substitute for an independent production security review.

## API

The control plane exposes a versioned JSON API under `/api/v1`. Errors use RFC 7807 `application/problem+json`.

| Area | Endpoints |
|---|---|
| Health | `GET /healthz`, `GET /api/v1/healthz` |
| Auth | `GET /api/v1/me`, `POST /api/v1/auth/dev-login`, `POST /api/v1/auth/logout` |
| Organisations | `GET /api/v1/orgs/{slug}`, `GET /api/v1/orgs/{slug}/usage`, `GET /api/v1/plans`, `POST /api/v1/orgs` |
| Projects | `GET/POST /api/v1/projects`, `GET/PATCH/DELETE /api/v1/projects/{id}` |
| Deployments | `POST /api/v1/projects/{id}/deployments`, `GET /api/v1/projects/{id}/deployments`, `GET /api/v1/deployments/{id}`, cancel, promote, graph, logs |
| Secrets | `GET /api/v1/projects/{id}/secrets`, `PUT/DELETE /api/v1/projects/{id}/secrets/{key}` |
| Domains | `GET/POST /api/v1/projects/{id}/domains`, `POST /api/v1/domains/{id}/verify`, `DELETE /api/v1/domains/{id}` |
| Incidents | `GET /api/v1/orgs/{slug}/incidents`, `GET /api/v1/projects/{id}/incidents`, `GET /api/v1/incidents/{id}`, evidence, timeline, actions |

Interactive documentation is served at `/api/v1/docs`; the OpenAPI document is at `/api/v1/openapi.json`.

## Autopilot research plane

The Autopilot track is the research contribution: a reproducible fault-injection harness for evaluating incident diagnosis. The current implementation is deliberately offline and does not mutate a cluster.

```bash
uv run python -m autopilot.inject.cli verify
uv run python -m autopilot.inject.cli accounts
uv run python -m autopilot.inject.cli plan --out .local/spec30-plan
```

- `verify` checks the frozen train/held-out partition and the test-application catalogue.
- `accounts` prints injection counts and the deterministic plan digest.
- `plan` renders the full run set plus Chaos Mesh, Locust, and runbook artifacts.

The live cluster adapter, fault-injection loop, and model evaluation pipeline are roadmap items. Console incidents are currently seeded records, not live Autopilot output.

## Roadmap

- [ ] Wire GitHub OAuth, repository connection, webhook delivery, and HEAD resolution for manual deployments.
- [ ] Prove Dockerfile and buildpack application shapes end-to-end.
- [ ] Implement the Autopilot live cluster adapter and fault-injection loop; replace seeded incidents with generated evidence.
- [ ] Complete public DNS verification, certificate issuance, and custom-domain routing.
- [ ] Add AWS k3s manifests, production registry push, in-cluster auth, and worker DSN separation.
- [ ] Replace the billing, reports, and admin shells with production implementations.
- [ ] Add continuous integration for the Python suite, console typecheck, lint, tests, and production build.

## License

Released under the [MIT License](LICENSE).

Copyright (c) 2026 Danish Naseer.

## Contributing

1. Create a branch for the change.
2. Keep the smallest diff that satisfies the behaviour.
3. Run `uv run pytest` and the console checks above.
4. Open a pull request with the problem, the approach, and the verification evidence.

Do not commit credentials, `.env` files, master keys, or generated local artifacts.

<div align="center">
  <sub>AutoPilot · MIT licensed · FastAPI · React · PostgreSQL · Kubernetes</sub>
</div>
