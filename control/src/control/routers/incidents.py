"""Read-only incident surface plus the two human approval actions (SPEC-36 §3.7)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from control import db as db_module
from control.errors import ApiProblem, not_found
from control.models import (
    ActionOut,
    ClaimOut,
    DiagnosisOut,
    EvidenceOut,
    IncidentListOut,
    IncidentOut,
    IncidentSummaryOut,
    TimelineEventOut,
    TimelineOut,
)
from control.pagination import clamp_limit, decode_cursor, encode_cursor
from control.security import (
    AuthContext,
    get_auth,
    require_admin,
    resolve_incident,
    resolve_org,
)

router = APIRouter(tags=["incidents"])
Auth = Annotated[AuthContext, Depends(get_auth)]
_STATUSES = {"open", "diagnosed", "actioning", "resolved", "escalated"}


def _action(row) -> ActionOut:
    return ActionOut(
        id=str(row["id"]),
        action=row["action"],
        target=row["target"],
        params=dict(row["params"] or {}),
        status=row["status"],
        requires_approval=row["requires_approval"],
        approved_by=str(row["approved_by"]) if row["approved_by"] else None,
        approved_at=row["approved_at"],
        approval_expires_at=row["approval_expires_at"],
        executed_at=row["executed_at"],
    )


def _candidate_list(candidates) -> list[dict]:
    if isinstance(candidates, dict):
        candidates = list(candidates.values())
    if not isinstance(candidates, list):
        return []
    return [
        item for item in candidates if isinstance(item, dict)
    ]


def _candidate_evidence(candidates) -> list[dict]:
    evidence: list[dict] = []
    for candidate in sorted(
        _candidate_list(candidates),
        key=lambda item: item.get("rank", 0),
    ):
        for ref in candidate.get("evidence") or []:
            if isinstance(ref, dict) and "kind" in ref and "ref" in ref:
                evidence.append(ref)
    return evidence


def _claim_rows(claims) -> list[dict]:
    if isinstance(claims, dict):
        claims = list(claims.values())
    if not isinstance(claims, list):
        return []
    return [item for item in claims if isinstance(item, dict) and "claim" in item]


def _flatten_evidence(row) -> list[dict]:
    """Flat 1-based citation order: ranked causes, then diagnosis claims."""
    return [
        *_candidate_evidence(row["candidates"]),
        *[
            ref
            for item in _claim_rows(row["claims"])
            for ref in (item.get("evidence") or [])
            if isinstance(ref, dict) and "kind" in ref and "ref" in ref
        ],
    ]


def _diagnosis(row) -> DiagnosisOut | None:
    if row is None:
        return None
    candidates = row["candidates"] or []
    claims: list[ClaimOut] = []
    offset = len(_candidate_evidence(candidates))
    for item in _claim_rows(row["claims"]):
        evidence: list[EvidenceOut] = []
        for ref in item.get("evidence") or []:
            if not isinstance(ref, dict) or "kind" not in ref or "ref" not in ref:
                continue
            payload = {key: value for key, value in ref.items() if key != "n"}
            evidence.append(EvidenceOut(n=offset + len(evidence) + 1, **payload))
        claims.append(ClaimOut(claim=item["claim"], evidence=evidence))
    return DiagnosisOut(
        id=str(row["id"]),
        summary=row["summary"],
        confidence=float(row["confidence"]),
        candidates=candidates,
        claims=claims,
        model_version=row["model_version"],
        corpus_version=row["corpus_version"],
        created_at=row["created_at"],
    )


def _summary(row) -> IncidentSummaryOut:
    return IncidentSummaryOut(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        deployment_id=str(row["deployment_id"]) if row["deployment_id"] else None,
        status=row["status"],
        severity=row["severity"],
        detected_at=row["detected_at"],
        detected_by=row["detected_by"],
        resolved_at=row["resolved_at"],
        resolved_by=row["resolved_by"],
        recovery_verified=row["recovery_verified"],
        summary=row["diagnosis_summary"],
        fault_signature=row["fault_signature"],
    )


async def _load_incident(conn, incident_id: str, org_id: str):
    row = await conn.fetchrow(
        """
        SELECT i.*, d.summary AS diagnosis_summary
          FROM incidents i
          LEFT JOIN diagnoses d ON d.incident_id = i.id
         WHERE i.id = $1 AND i.org_id = $2
        """,
        uuid.UUID(incident_id),
        uuid.UUID(org_id),
    )
    if row is None:
        raise not_found("incident")
    return row


@router.get("/orgs/{slug}/incidents", response_model=IncidentListOut)
async def list_incidents(
    request: Request,
    slug: str,
    auth: Auth,
    status: str | None = None,
    severity: str | None = None,
    cursor: str | None = None,
    limit: int = 25,
) -> IncidentListOut:
    access = await resolve_org(request, auth, slug)
    if status is not None and status not in _STATUSES:
        raise ApiProblem(
            422,
            "validation_failed",
            "unknown incident status",
            fields={"status": "invalid status"},
        )
    db: db_module.Db = request.app.state.db
    take = clamp_limit(limit)
    params: list[object] = [uuid.UUID(access.org_id)]
    where = ["i.org_id = $1"]
    if status is not None:
        params.append(status)
        where.append(f"i.status = ${len(params)}")
    if severity is not None:
        params.append(severity)
        where.append(f"i.severity = ${len(params)}")
    if cursor:
        detected_at, row_id = decode_cursor(cursor)
        params.extend([detected_at, row_id])
        where.append(f"(i.detected_at, i.id) < (${len(params)-1}, ${len(params)})")
    params.append(take + 1)
    async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
        rows = await conn.fetch(
            f"""
            SELECT i.*, d.summary AS diagnosis_summary
              FROM incidents i
              LEFT JOIN diagnoses d ON d.incident_id = i.id
             WHERE {' AND '.join(where)}
             ORDER BY i.detected_at DESC, i.id DESC
             LIMIT ${len(params)}
            """,
            *params,
        )
    # The list cursor is ordered by detected_at, not created_at.
    items = [_summary(row) for row in rows[:take]]
    next_token = None
    if len(rows) > take:
        last = rows[take - 1]
        next_token = encode_cursor(last["detected_at"], last["id"])
    return IncidentListOut(items=items, next_cursor=next_token)


@router.get("/projects/{project_id}/incidents", response_model=IncidentListOut)
async def list_project_incidents(
    request: Request,
    project_id: str,
    auth: Auth,
    status: str | None = None,
    severity: str | None = None,
    limit: int = 25,
) -> IncidentListOut:
    from control.security import resolve_project

    access = await resolve_project(request, auth, project_id)
    db: db_module.Db = request.app.state.db
    take = clamp_limit(limit)
    params: list[object] = [uuid.UUID(project_id), uuid.UUID(access.org_id)]
    where = ["i.project_id = $1", "i.org_id = $2"]
    if status is not None:
        params.append(status)
        where.append(f"i.status = ${len(params)}")
    if severity is not None:
        params.append(severity)
        where.append(f"i.severity = ${len(params)}")
    params.append(take)
    async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
        rows = await conn.fetch(
            f"""
            SELECT i.*, d.summary AS diagnosis_summary
              FROM incidents i
              LEFT JOIN diagnoses d ON d.incident_id = i.id
             WHERE {' AND '.join(where)}
             ORDER BY i.detected_at DESC, i.id DESC
             LIMIT ${len(params)}
            """,
            *params,
        )
    return IncidentListOut(items=[_summary(row) for row in rows[:take]], next_cursor=None)


@router.get("/incidents/{incident_id}", response_model=IncidentOut)
async def incident_detail(
    request: Request, incident_id: str, auth: Auth
) -> IncidentOut:
    access = await resolve_incident(request, auth, incident_id)
    db: db_module.Db = request.app.state.db
    async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
        row = await _load_incident(conn, incident_id, access.org_id)
        diagnosis_row = await conn.fetchrow(
            "SELECT * FROM diagnoses WHERE incident_id = $1", uuid.UUID(incident_id)
        )
        actions = await conn.fetch(
            "SELECT * FROM incident_actions WHERE incident_id = $1 "
            "ORDER BY id",
            uuid.UUID(incident_id),
        )
    base = _summary(row)
    return IncidentOut(
        **base.model_dump(),
        diagnosis=_diagnosis(diagnosis_row),
        actions=[_action(action) for action in actions],
    )


@router.get("/incidents/{incident_id}/evidence/{n}", response_model=EvidenceOut)
async def incident_evidence(
    request: Request, incident_id: str, n: int, auth: Auth
) -> EvidenceOut:
    if n < 1:
        raise ApiProblem(
            422, "validation_failed", "evidence index is 1-based", fields={"n": "must be >= 1"}
        )
    access = await resolve_incident(request, auth, incident_id)
    db: db_module.Db = request.app.state.db
    async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM diagnoses WHERE incident_id = $1", uuid.UUID(incident_id)
        )
    if row is None:
        raise not_found("evidence")
    evidence = _flatten_evidence(row)
    if n > len(evidence):
        raise not_found("evidence")
    ev = EvidenceOut(n=n, **evidence[n - 1])
    # A metric/log/trace source may not be connected in this environment. The
    # citation still resolves structurally; `resolved` tells the console whether
    # the value was actually fetched.
    resolved = ev.value is not None
    return ev.model_copy(
        update={
            "resolved": resolved,
            "unsupported_reason": (
                None if resolved else "source not connected in this environment"
            ),
        }
    )


@router.get("/incidents/{incident_id}/timeline", response_model=TimelineOut)
async def incident_timeline(
    request: Request, incident_id: str, auth: Auth
) -> TimelineOut:
    access = await resolve_incident(request, auth, incident_id)
    db: db_module.Db = request.app.state.db
    async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
        incident = await _load_incident(conn, incident_id, access.org_id)
        diagnosis = await conn.fetchrow(
            "SELECT * FROM diagnoses WHERE incident_id = $1", uuid.UUID(incident_id)
        )
        actions = await conn.fetch(
            "SELECT * FROM incident_actions WHERE incident_id = $1 ORDER BY id",
            uuid.UUID(incident_id),
        )
    events: list[TimelineEventOut] = [
        TimelineEventOut(
            at=incident["detected_at"],
            kind="detected",
            title=f"Incident detected by {incident['detected_by']}",
            detail=f"severity {incident['severity']}",
            actor=incident["detected_by"],
        )
    ]
    if diagnosis is not None:
        events.append(
            TimelineEventOut(
                at=diagnosis["created_at"],
                kind="diagnosis",
                title="Diagnosis produced",
                detail=diagnosis["summary"],
                actor=diagnosis["model_version"],
            )
        )
    for action in actions:
        events.append(
            TimelineEventOut(
                at=action["executed_at"] or action["approved_at"] or incident["detected_at"],
                kind="action",
                title=f"{action['action']} {action['status']}",
                detail=f"target {action['target']}",
                actor=str(action["approved_by"]) if action["approved_by"] else "agent",
            )
        )
    if incident["resolved_at"] is not None:
        events.append(
            TimelineEventOut(
                at=incident["resolved_at"],
                kind="resolved",
                title="Incident resolved",
                detail=f"recovery verified: {incident['recovery_verified']}",
                actor=incident["resolved_by"],
            )
        )
    events.sort(key=lambda event: event.at)
    return TimelineOut(items=events)


async def _decide_action(
    request: Request,
    incident_id: str,
    action_id: str,
    auth: AuthContext,
    *,
    approve: bool,
) -> ActionOut:
    access = await resolve_incident(request, auth, incident_id)
    require_admin(access)
    db: db_module.Db = request.app.state.db
    async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
        action = await conn.fetchrow(
            "SELECT * FROM incident_actions WHERE id = $1 AND incident_id = $2 "
            "AND org_id = $3 FOR UPDATE",
            uuid.UUID(action_id),
            uuid.UUID(incident_id),
            uuid.UUID(access.org_id),
        )
        if action is None:
            raise not_found("action")
        if action["status"] != "pending_approval":
            raise ApiProblem(
                409,
                "conflict",
                "only an action waiting for approval can be approved or rejected",
            )
        await db_module.write_audit(
            db,
            action="incident.action_approve" if approve else "incident.action_reject",
            actor_type="user",
            actor_id=auth.user_id,
            org_id=access.org_id,
            target_type="incident_action",
            target_id=action_id,
        )
        updated = await conn.fetchrow(
            """
            UPDATE incident_actions
               SET status = $1,
                   approved_by = $2,
                   approved_at = now()
             WHERE id = $3
             RETURNING *
            """,
            "approved" if approve else "rejected",
            uuid.UUID(auth.user_id),
            uuid.UUID(action_id),
        )
    return _action(updated)


@router.post(
    "/incidents/{incident_id}/actions/{action_id}/approve",
    response_model=ActionOut,
)
async def approve_action(
    request: Request, incident_id: str, action_id: str, auth: Auth
) -> ActionOut:
    return await _decide_action(
        request, incident_id, action_id, auth, approve=True
    )


@router.post(
    "/incidents/{incident_id}/actions/{action_id}/reject",
    response_model=ActionOut,
)
async def reject_action(
    request: Request, incident_id: str, action_id: str, auth: Auth
) -> ActionOut:
    return await _decide_action(
        request, incident_id, action_id, auth, approve=False
    )
