"""Build-egress policy and NetworkPolicy generation (SPEC-10 R-12..R-14).

This module models the policy and produces the Kubernetes NetworkPolicy shape
used by the deployment plane. It intentionally rejects allowlist entries that
overlap cloud metadata, RFC1918 or the platform's own CIDRs: an allowlist is an
exception to default-deny, not permission to bypass the protected ranges.

Actual packet enforcement and denial logging belong to the cluster CNI/egress
proxy; ``record_egress_denial`` is the write side that adapter must call. The
coordinator owns the cluster verification for AC-9/AC-10 (see DECISIONS).
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Network, ip_address, ip_network
from typing import Iterable, Mapping

from buildplane.supplychain.audit import (
    ACTION_EGRESS_DENIED,
    AuditLog,
    AuditRecord,
)

METADATA_NETWORK = ip_network("169.254.0.0/16")
RFC1918_NETWORKS = (
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
)
HARD_BLOCKED: tuple[tuple[IPv4Network, str], ...] = (
    (METADATA_NETWORK, "cloud metadata/link-local"),
    *((network, "RFC1918") for network in RFC1918_NETWORKS),
)


@dataclass(frozen=True, slots=True)
class EgressRule:
    cidr: str
    ports: tuple[int, ...]

    def __post_init__(self) -> None:
        try:
            network = ip_network(self.cidr, strict=False)
        except ValueError as exc:
            raise ValueError(f"invalid egress CIDR: {self.cidr!r}") from exc
        if network.version != 4:
            raise ValueError("build egress allowlist currently accepts IPv4 only")
        if not self.ports:
            raise ValueError("egress rule needs at least one port")
        for port in self.ports:
            if not 1 <= int(port) <= 65535:
                raise ValueError(f"invalid egress port: {port!r}")
        object.__setattr__(self, "ports", tuple(int(port) for port in self.ports))

    @property
    def network(self) -> IPv4Network:
        return ip_network(self.cidr, strict=False)


@dataclass(frozen=True, slots=True)
class EgressDecision:
    destination: str
    port: int
    allowed: bool
    reason: str


class EgressPolicy:
    def __init__(
        self,
        *,
        allow: Iterable[EgressRule] = (),
        platform_cidrs: Iterable[str] = (),
    ) -> None:
        self.allow = tuple(allow)
        self.platform_cidrs = tuple(
            ip_network(cidr, strict=False) for cidr in platform_cidrs
        )
        for rule in self.allow:
            if rule.network.version != 4:
                raise ValueError("build egress allowlist currently accepts IPv4 only")
            conflict = self._hard_block_reason(ip_address(rule.network.network_address))
            if conflict is None and rule.network.prefixlen < rule.network.max_prefixlen:
                # A broad rule such as 0.0.0.0/0 could reach a blocked range even
                # if its first address is public. Check overlap explicitly.
                for blocked, reason in HARD_BLOCKED:
                    if rule.network.overlaps(blocked):
                        conflict = reason
                        break
            if conflict is None:
                for blocked in self.platform_cidrs:
                    if rule.network.overlaps(blocked):
                        conflict = "platform CIDR"
                        break
            if conflict is not None:
                raise ValueError(
                    f"egress allowlist {rule.cidr} overlaps protected {conflict} range"
                )

    def check(self, destination: str, port: int) -> EgressDecision:
        try:
            address = ip_address(destination)
        except ValueError:
            return EgressDecision(destination, int(port), False, "not an IP address")
        if address.version != 4:
            return EgressDecision(destination, int(port), False, "IPv6 not allowlisted")
        reason = self._hard_block_reason(address)
        if reason is not None:
            return EgressDecision(destination, int(port), False, reason)
        for blocked in self.platform_cidrs:
            if address in blocked:
                return EgressDecision(
                    destination, int(port), False, "platform CIDR"
                )
        for rule in self.allow:
            if address in rule.network and int(port) in rule.ports:
                return EgressDecision(destination, int(port), True, "allowlisted")
        return EgressDecision(destination, int(port), False, "default deny")

    def to_network_policy(
        self,
        *,
        name: str = "build-plane-egress",
        namespace: str | None = None,
        pod_labels: Mapping[str, str] | None = None,
    ) -> dict:
        metadata: dict[str, str] = {"name": name}
        if namespace is not None:
            metadata["namespace"] = namespace
        egress = [
            {
                "to": [{"ipBlock": {"cidr": rule.cidr}}],
                "ports": [
                    {"protocol": "TCP", "port": port} for port in rule.ports
                ],
            }
            for rule in self.allow
        ]
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": metadata,
            "spec": {
                "podSelector": {"matchLabels": dict(pod_labels or {"plane": "build"})},
                "policyTypes": ["Egress"],
                "egress": egress,
            },
        }

    @staticmethod
    def _hard_block_reason(address) -> str | None:
        for network, reason in HARD_BLOCKED:
            if address in network:
                return reason
        return None


async def record_egress_denial(
    audit_log: AuditLog,
    claim,
    decision: EgressDecision,
) -> None:
    if decision.allowed:
        return
    await audit_log.write(
        AuditRecord(
            action=ACTION_EGRESS_DENIED,
            actor_type="system",
            actor_id="build-egress",
            org_id=claim.org_id,
            target_type="deployment",
            target_id=claim.deployment_id,
            metadata={
                "destination": decision.destination,
                "port": decision.port,
                "reason": decision.reason,
                "project_id": claim.project_id,
                "build_id": claim.job_id,
            },
        )
    )
