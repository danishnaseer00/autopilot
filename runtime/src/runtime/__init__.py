"""AutoPilot runtime plane (Worker B).

Modules map to the build order:

* ``secrets``    - SPEC-18 envelope encryption, build/runtime resolution, audit
* ``lifecycle``  - SPEC-13 deployment state machine, superseding, promotion
* ``k8srender``  - SPEC-14 pure manifest -> Kubernetes objects, agent actions
* ``ingress``    - SPEC-15 hosts, certificates, Ingress rendering
* ``routing``    - SPEC-16 resolution semantics, reconciler, edge rate limits

Everything under ``k8srender`` is pure (SPEC-14 R-9): no cluster calls, clock,
or randomness. Cluster behaviour is verified by the coordinator, not here.
"""

__all__: list[str] = []
