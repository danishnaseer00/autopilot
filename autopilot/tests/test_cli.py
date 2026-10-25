"""Offline CLI contract for the SPEC-30 harness (no cluster, no network)."""

from __future__ import annotations

import json

import yaml

from autopilot.inject import cli
from autopilot.inject.partition import HELDOUT_V1_SHA256
from autopilot.inject.runbook import RUNBOOK_ACTIONS
from autopilot.inject.testapps import TESTAPPS_V1_SHA256


def test_verify_reports_the_frozen_partition_and_catalogue(capsys) -> None:
    assert cli.main(["verify"]) == 0
    output = capsys.readouterr().out
    assert HELDOUT_V1_SHA256 in output
    assert TESTAPPS_V1_SHA256 in output
    assert "heldout F4, F9, F10" in output
    assert "magnitude F3.intensity" in output
    assert "OK: committed partitions are frozen and disjoint" in output


def test_verify_json_is_machine_readable(capsys) -> None:
    assert cli.main(["verify", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["partition"]["sha256"] == HELDOUT_V1_SHA256
    assert payload["partition"]["heldout_types"] == ["F4", "F9", "F10"]
    assert payload["test_apps"]["sha256"] == TESTAPPS_V1_SHA256
    assert payload["test_apps"]["apps"][0]["app_id"] == "storefront"
    assert payload["test_apps"]["apps"][0]["graph_edges"] == 2


def test_plan_writes_a_complete_plan_and_limited_artifacts(tmp_path, capsys) -> None:
    out = tmp_path / "plan"
    assert cli.main(["plan", "--out", str(out), "--artifacts-limit", "2"]) == 0
    output = capsys.readouterr().out
    assert "main=300 magnitude=390 total=690 artifacts=2" in output

    plan_document = json.loads((out / "plan.json").read_text(encoding="utf-8"))
    assert plan_document["schema_version"] == 1
    assert plan_document["plan_sha256"]
    assert len(plan_document["runs"]) == 690
    assert plan_document["counts"]["main_total"] == 300
    assert plan_document["partition"]["sha256"] == HELDOUT_V1_SHA256

    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["artifacts_written"] == 2
    assert summary["artifacts_limit"] == 2
    assert summary["plan_sha256"] == plan_document["plan_sha256"]
    assert summary["testapps_sha256"] == TESTAPPS_V1_SHA256
    assert summary["counts"]["total"] == 690

    run_dirs = sorted(path for path in (out / "runs").iterdir() if path.is_dir())
    assert len(run_dirs) == 2
    for run_dir in run_dirs:
        for name in (
            "run.json",
            "chaos.yaml",
            "locustfile.py",
            "load-profile.json",
            "runbook.json",
        ):
            assert (run_dir / name).exists(), name
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        assert run["namespace"] == "autopilot-test-storefront"
        assert run["fault_type"] == "F1"
        manifest = yaml.safe_load((run_dir / "chaos.yaml").read_text(encoding="utf-8"))
        assert manifest["kind"] == "PodChaos"
        assert manifest["metadata"]["namespace"] == run["namespace"]
        assert manifest["spec"]["selector"]["namespaces"] == [run["namespace"]]
        runbook = json.loads((run_dir / "runbook.json").read_text(encoding="utf-8"))
        assert runbook["actions"] == list(RUNBOOK_ACTIONS)
        assert "class AutopilotFaultLoad" in (run_dir / "locustfile.py").read_text(
            encoding="utf-8"
        )


def test_plan_summary_is_reproducible_for_the_same_seed(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    assert cli.main(["plan", "--out", str(first), "--artifacts-limit", "0"]) == 0
    assert cli.main(["plan", "--out", str(second), "--artifacts-limit", "0"]) == 0
    first_plan = json.loads((first / "plan.json").read_text(encoding="utf-8"))
    second_plan = json.loads((second / "plan.json").read_text(encoding="utf-8"))
    assert first_plan["plan_sha256"] == second_plan["plan_sha256"]
    assert not (first / "runs").exists()
    assert not (second / "runs").exists()


def test_accounts_prints_the_counts_and_digest(capsys) -> None:
    assert cli.main(["accounts"]) == 0
    output = capsys.readouterr().out
    assert '"main_total": 300' in output
    assert '"total": 690' in output
    assert f"partition_sha256 {HELDOUT_V1_SHA256}" in output
    assert "plan_sha256" in output


def test_plan_rejects_a_platform_namespace(tmp_path, capsys) -> None:
    assert (
        cli.main(
            [
                "plan",
                "--out",
                str(tmp_path / "bad"),
                "--namespace",
                "kube-system",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "platform namespace" in captured.err
    assert not (tmp_path / "bad").exists()


def test_plan_rejects_too_few_repetitions(tmp_path, capsys) -> None:
    assert (
        cli.main(["plan", "--out", str(tmp_path / "small"), "--repetitions", "5"])
        == 2
    )
    assert "repetitions must be >= 30" in capsys.readouterr().err


def test_plan_rejects_a_negative_artifact_limit(tmp_path, capsys) -> None:
    out = tmp_path / "negative"
    assert cli.main(["plan", "--out", str(out), "--artifacts-limit", "-1"]) == 2
    captured = capsys.readouterr()
    assert "artifacts-limit" in captured.err
    # The plan itself was valid and was written before rendering failed.
    assert (out / "plan.json").exists()
    assert not (out / "runs").exists()
