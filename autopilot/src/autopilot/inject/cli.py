"""Command line for the SPEC-30 harness.

Commands are deliberately offline: ``verify`` checks the frozen partition and
test-app catalogue, ``plan`` renders the full run set plus Chaos Mesh/Locust/
runbook artifacts, and ``accounts`` prints the injection accounting.  No
command talks to a cluster.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

import yaml

from autopilot.inject.chaos import render_fault_artifact
from autopilot.inject.errors import FaultInjectionError
from autopilot.inject.load import render_locustfile
from autopilot.inject.partition import HELDOUT_V1_SHA256, load_partition
from autopilot.inject.plan import (
    DEFAULT_MAGNITUDE_REPETITIONS,
    DEFAULT_NAMESPACE,
    DEFAULT_REPETITIONS,
    build_plan,
    plan_digest,
)
from autopilot.inject.runbook import build_runbook
from autopilot.inject.testapps import TESTAPPS_V1_SHA256, load_test_app_catalogue


def _write_json(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m autopilot.inject.cli",
        description="SPEC-30 fault-injection catalogue, plan and artifact generator",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    verify = sub.add_parser("verify", help="verify frozen partition and app catalogue")
    verify.add_argument("--json", action="store_true", help="print machine-readable output")

    plan = sub.add_parser("plan", help="build a plan and render cluster artifacts")
    plan.add_argument("--out", required=True, type=Path, help="artifact directory")
    plan.add_argument("--seed", type=int, default=20261023)
    plan.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    plan.add_argument(
        "--magnitude-repetitions", type=int, default=DEFAULT_MAGNITUDE_REPETITIONS
    )
    plan.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    plan.add_argument(
        "--artifacts-limit",
        type=int,
        default=None,
        help="render artifacts for the first N runs only (the plan remains complete)",
    )

    accounts = sub.add_parser("accounts", help="print injection counts and plan digest")
    accounts.add_argument("--seed", type=int, default=20261023)
    accounts.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    accounts.add_argument(
        "--magnitude-repetitions", type=int, default=DEFAULT_MAGNITUDE_REPETITIONS
    )
    accounts.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    return parser


def _verify(as_json: bool) -> int:
    partition = load_partition()
    catalogue = load_test_app_catalogue()
    payload = {
        "partition": {
            "version": partition.version,
            "sha256": partition.sha256,
            "train_types": list(partition.train_types),
            "heldout_types": list(partition.heldout_types),
            "regimes": {
                name: {
                    "train": list(partition.regime(name).train_types),
                    "test": list(partition.regime(name).test_types),
                }
                for name in ("seen_type", "unseen_type")
            },
            "magnitude_holdout": {
                fault: {
                    "parameter": split.parameter,
                    "train": list(split.train_values),
                    "test": list(split.test_values),
                }
                for fault, split in partition.magnitude_holdout.items()
            },
        },
        "test_apps": {
            "catalogue_id": catalogue.catalogue_id,
            "sha256": catalogue.sha256,
            "apps": [
                {
                    "app_id": app.app_id,
                    "version": app.version,
                    "services": [service.name for service in app.services],
                    "graph_nodes": len(app.graph.nodes),
                    "graph_edges": len(app.graph.edges),
                }
                for app in catalogue.apps
            ],
        },
    }
    if as_json:
        print(json.dumps(payload, sort_keys=True, indent=2))
    else:
        print(
            f"partition v{partition.version} {partition.sha256} "
            f"(heldout {', '.join(partition.heldout_types)})"
        )
        print(
            f"test-app catalogue {catalogue.catalogue_id} {catalogue.sha256} "
            f"({len(catalogue.apps)} app, {len(catalogue.apps[0].services)} services)"
        )
        for fault, split in sorted(partition.magnitude_holdout.items()):
            print(
                f"magnitude {fault}.{split.parameter}: "
                f"train={list(split.train_values)} test={list(split.test_values)}"
            )
        print("OK: committed partitions are frozen and disjoint")
    return 0


def _build(args: argparse.Namespace):
    return build_plan(
        seed=args.seed,
        repetitions=args.repetitions,
        magnitude_repetitions=args.magnitude_repetitions,
        namespace=args.namespace,
    )


def _write_artifacts(plan, out: Path, limit: int | None) -> int:
    app = plan.app
    runs = plan.all_runs
    if limit is not None:
        if limit < 0:
            raise FaultInjectionError("--artifacts-limit must be >= 0")
        runs = runs[:limit]
    for run in runs:
        run_dir = out / "runs" / run.run_id
        _write_json(run_dir / "run.json", run.to_dict())
        filename, document = render_fault_artifact(run, app)
        if filename.endswith(".yaml"):
            _write_text(
                run_dir / filename,
                yaml.safe_dump(document, sort_keys=True, default_flow_style=False),
            )
        else:
            _write_json(run_dir / filename, document)
        _write_text(run_dir / "locustfile.py", render_locustfile(run.load_profile))
        _write_json(run_dir / "load-profile.json", run.load_profile.to_dict())
        _write_json(run_dir / "runbook.json", build_runbook(run).to_dict())
    return len(runs)


def _plan(args: argparse.Namespace) -> int:
    plan = _build(args)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    digest = plan_digest(plan)
    plan_document = {"plan_sha256": digest, **plan.to_dict()}
    _write_json(out / "plan.json", plan_document)
    written = _write_artifacts(plan, out, args.artifacts_limit)
    summary = {
        "plan_sha256": digest,
        "partition_sha256": plan.partition.sha256,
        "testapps_sha256": TESTAPPS_V1_SHA256,
        "seed": plan.seed,
        "namespace": plan.namespace,
        "counts": plan.counts(),
        "artifacts_written": written,
        "artifacts_limit": args.artifacts_limit,
    }
    _write_json(out / "summary.json", summary)
    print(f"plan {digest}")
    print(
        f"main={len(plan.main_runs)} magnitude={len(plan.magnitude_runs)} "
        f"total={len(plan.all_runs)} artifacts={written}"
    )
    print(f"wrote {out}")
    return 0


def _accounts(args: argparse.Namespace) -> int:
    plan = _build(args)
    print(json.dumps(plan.counts(), sort_keys=True, indent=2))
    print(f"plan_sha256 {plan_digest(plan)}")
    print(f"partition_sha256 {plan.partition.sha256}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            return _verify(args.json)
        if args.command == "plan":
            return _plan(args)
        if args.command == "accounts":
            return _accounts(args)
    except FaultInjectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
