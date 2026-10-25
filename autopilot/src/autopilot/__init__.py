"""AutoPilot research plane (SPEC-30..35).

SPEC-30 owns fault injection and ground truth.  The package is deliberately
pure Python: catalogue, partition, planning, manifest rendering and the
write-once ground-truth store all run without a cluster, a database or an ML
stack.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
