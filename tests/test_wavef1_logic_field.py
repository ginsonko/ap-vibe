from __future__ import annotations

from pathlib import Path
import json
import threading
from urllib import error as urllib_error
from urllib import request as urllib_request

import pytest

from ap_mind.contracts import ContractError
from ap_mind.logic_field import LogicFieldInstrument, LogicFieldPolicy, LogicQuery
from ap_mind.studio_server import create_server


def _project(root: Path) -> Path:
    files = {
        "src/sample/__init__.py": "from .core import leaf\n",
        "src/sample/core.py": '''"""Core sample responsibilities."""

def leaf(value):
    """Return the observed value."""
    return value

class Worker:
    """Coordinate one bounded unit of work."""
    def helper(self, value):
        return leaf(value)

    def run(self, value):
        return self.helper(value)

def dynamic(name, value):
    return getattr(Worker(), name)(value)

def unused_public():
    return "possible external entry"
''',
        "src/sample/consumer.py": "from sample.core import leaf\n\ndef consume(value):\n    return leaf(value)\n",
        "src/sample/other.py": "def leaf(value):\n    return value\n",
        "tests/test_core.py": "from sample.core import leaf\n\ndef test_leaf():\n    assert leaf(1) == 1\n",
        "web/view.jsx": "export function View(){ return null }\n",
        "src/sample/broken.py": "def broken(:\n    pass\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def _query(kind: str, target: str, **extra) -> LogicQuery:
    return LogicQuery(
        query_id=f"query-{kind}-{target}",
        project_id="project-local",
        kind=kind,
        target=target,
        source_ref="user://logic-field/test",
        **extra,
    )


def test_module_responsibility_keeps_static_facts_unknowns_and_axes_separate(tmp_path: Path) -> None:
    instrument = LogicFieldInstrument(_project(tmp_path / "project"))
    report = instrument.query(_query("module_responsibility", "sample.core.Worker"))

    assert report.status == "success"
    assert any(node.qualname == "sample.core.Worker" for node in report.nodes)
    assert any(node.qualname == "sample.core.Worker.run" for node in report.nodes)
    assert any(edge.kind == "calls" for edge in report.edges)
    assert report.snapshot.unsupported_source_files == 1
    assert report.snapshot.failed_files[0]["code"] == "python_syntax_error"
    assert report.completeness == "partial"
    assert report.logic_axes == {
        "subjective_closure": "not_assessed",
        "execution_consistency": "unknown",
        "formal_validity": "static_structure_only",
        "empirical_truth": "unknown",
    }
    assert all(not node.path.startswith(str(tmp_path)) for node in report.nodes)
    assert "static_reachability_is_not_runtime_impact_or_bug_proof" in report.limitations


def test_change_impact_returns_potential_consumers_not_runtime_failure(tmp_path: Path) -> None:
    instrument = LogicFieldInstrument(_project(tmp_path / "project"))
    report = instrument.query(_query("change_impact", "sample.core.leaf", max_depth=4))

    names = {node.qualname for node in report.nodes}
    assert "sample.core.leaf" in names
    assert "sample.core.Worker.helper" in names
    assert "sample.consumer.consume" in names
    assert "tests.test_core.test_leaf" in names
    assert {edge.kind for edge in report.edges} >= {"calls", "tests"}
    assert "潜在消费者" in report.summary
    assert "不是运行故障" in report.summary
    assert report.logic_axes["empirical_truth"] == "unknown"


def test_first_broken_link_distinguishes_connected_missing_and_ambiguous(tmp_path: Path) -> None:
    instrument = LogicFieldInstrument(_project(tmp_path / "project"))
    connected = instrument.query(
        _query(
            "first_broken_link",
            "sample.core.Worker.run",
            expected_path=(
                "sample.core.Worker.run",
                "sample.core.Worker.helper",
                "sample.core.leaf",
            ),
        )
    )
    assert connected.status == "static_chain_observed"
    assert connected.first_break is None
    assert "不证明运行成功" in connected.summary

    missing = instrument.query(
        _query(
            "first_broken_link",
            "sample.core.Worker.run",
            expected_path=("sample.core.Worker.run", "sample.consumer.consume"),
        )
    )
    assert missing.status == "static_link_unobserved"
    assert missing.first_break["status"] == "static_link_unobserved"
    assert "尚不能判定" not in missing.summary
    assert "Bug" in missing.first_break["meaning"]

    ambiguous = instrument.query(_query("module_responsibility", "leaf"))
    assert ambiguous.status == "target_ambiguous"
    assert ambiguous.resolved_targets[0]["candidate_count"] == 2


def test_budget_exhaustion_is_search_incomplete_and_deterministic(tmp_path: Path) -> None:
    root = _project(tmp_path / "project")
    instrument = LogicFieldInstrument(root)
    query = _query("module_responsibility", "sample.core", max_nodes=4, max_edges=4, max_depth=1)
    first = instrument.query(query).to_dict()
    second = instrument.query(query).to_dict()

    assert first["report_id"] == second["report_id"]
    assert [item["node_id"] for item in first["nodes"]] == [item["node_id"] for item in second["nodes"]]
    assert first["completeness"] == "search_incomplete"
    assert first["budgets"]["limits_reached"]


@pytest.mark.parametrize(
    "target",
    ["../secret.py", "..\\secret.py", "C:" + "\\Users\\Administrator\\secret.py", "/etc/passwd"],
)
def test_query_rejects_targets_outside_fixed_root(tmp_path: Path, target: str) -> None:
    _project(tmp_path / "project")
    with pytest.raises(ContractError, match="logic_query_target_outside_configured_root"):
        _query("module_responsibility", target)


def test_index_policy_reports_file_budget_without_guessing(tmp_path: Path) -> None:
    root = _project(tmp_path / "project")
    instrument = LogicFieldInstrument(root, policy=LogicFieldPolicy(max_files=1))
    report = instrument.query(_query("module_responsibility", "sample"))
    assert report.snapshot.completeness == "search_incomplete"
    assert "file_budget_exhausted" in report.snapshot.limits_reached
    assert report.status in {"success", "target_unresolved"}


def test_source_docstrings_are_redacted_before_projection(tmp_path: Path) -> None:
    root = _project(tmp_path / "project")
    credential = "sk-" + "A" * 28
    source = root / "src/sample/credential_docs.py"
    source.write_text(
        f'''def configured():
    """Connect with api_key={credential} and Authorization: Bearer TOPSECRETTOKEN123."""
    return True
''',
        encoding="utf-8",
    )

    report = LogicFieldInstrument(root).query(
        _query("module_responsibility", "sample.credential_docs.configured")
    )
    projected = json.dumps(report.to_dict(), ensure_ascii=False)

    assert credential not in projected
    assert "TOPSECRETTOKEN123" not in projected
    assert "[REDACTED]" in projected


def _json_request(url: str, method: str = "GET", payload: dict | None = None):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    call = urllib_request.Request(
        url,
        data=raw,
        method=method,
        headers={"Content-Type": "application/json"} if raw is not None else {},
    )
    with urllib_request.urlopen(call, timeout=15) as response:
        return response.status, json.loads(response.read().decode("utf-8")), dict(response.headers)


def test_http_logic_query_runs_in_existing_ap_episode_replays_and_accepts_feedback(tmp_path: Path) -> None:
    source_root = _project(tmp_path / "project")
    server = create_server(
        port=0,
        data_dir=tmp_path / "data",
        codex_project_id="project-local",
        logic_root=source_root,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    payload = {
        "request_id": "logic-http-query",
        "query": {
            "query_id": "logic-http-query",
            "project_id": "project-local",
            "kind": "first_broken_link",
            "target": "sample.core.Worker.run",
            "expected_path": [
                "sample.core.Worker.run",
                "sample.core.Worker.helper",
                "sample.core.leaf",
            ],
            "max_nodes": 24,
            "max_edges": 48,
            "max_depth": 4,
            "include_tests": True,
            "source_ref": "user://logic-field/http",
        },
    }
    try:
        status, first, headers = _json_request(base + "/v1/ap-vibe/logic/query", "POST", payload)
        episode = first["episode"]
        assert status == 200
        assert first["replayed"] is False
        assert headers["X-AP-Mind-Replayed"] == "false"
        assert episode["episode_kind"] == "logic_field_observation"
        assert episode["logic_field"]["status"] == "static_chain_observed"
        assert episode["logic_field"]["logic_axes"]["subjective_closure"] == "not_assessed"
        assert episode["selected_action"] is not None
        assert len(episode["ticks"]) >= 1
        assert episode["ticks"][0]["decision"]["owner"] == "ap_native"
        assert episode["ticks"][0]["decision"]["result_status"] == "success"
        assert episode["counters"]["readbacks"] >= 1
        assert any(tick["timeline_kind"] == "readback" for tick in episode["ticks"][1:])
        assert episode["ownership"]["logic_action_winner"] == "native"
        assert episode["input"]["activity"]["kind"] == "logic_field_observation"
        assert episode["input"]["activity"]["instrument_sets_feeling"] is False
        assert episode["input"]["activity"]["instrument_sets_action_winner"] is False

        _, replay, replay_headers = _json_request(base + "/v1/ap-vibe/logic/query", "POST", payload)
        assert replay["replayed"] is True
        assert replay["episode"] == episode
        assert replay_headers["X-AP-Mind-Replayed"] == "true"

        feedback_payload = {
            "request_id": "logic-http-feedback",
            "feedback": {
                "feedback_id": "logic-http-feedback",
                "project_id": "project-local",
                "target_episode_id": episode["episode_id"],
                "target_action": episode["selected_action"]["kind"],
                "desired_action": "observe_only",
                "signal": "correction",
                "magnitude": 0.4,
                "natural_language": "静态链只能作为检查线索，不要直接暂存成事实。",
                "source_ref": "user://logic-field/http-feedback",
            },
        }
        _, feedback, _ = _json_request(base + "/v1/ap-vibe/feedback", "POST", feedback_payload)
        assert feedback["episode"]["episode_kind"] == "project_feedback"
        assert feedback["episode"]["ticks"]

        _, state, _ = _json_request(base + "/v1/ap-vibe/state")
        logic_records = [item for item in state["episodes"] if item["kind"] == "logic_query"]
        assert logic_records[0]["episode"]["logic_field"]["report_id"] == episode["logic_field"]["report_id"]
        _, health, _ = _json_request(base + "/v1/health")
        assert health["ap_vibe_api_version"] == "0.9.0"
        assert health["logic_field"]["configured"] is True
        assert health["logic_field"]["root_scope"] == "configured_project_root"
        assert "python" in health["logic_field"]["languages"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_logic_query_conflicts_when_snapshot_changes_and_rejects_unconfigured(tmp_path: Path) -> None:
    source_root = _project(tmp_path / "project")
    server = create_server(
        port=0,
        data_dir=tmp_path / "data",
        codex_project_id="project-local",
        logic_root=source_root,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    payload = {
        "request_id": "snapshot-fence",
        "query": {
            "query_id": "snapshot-fence",
            "project_id": "project-local",
            "kind": "change_impact",
            "target": "sample.core.leaf",
            "source_ref": "user://logic-field/snapshot-fence",
        },
    }
    try:
        _json_request(base + "/v1/ap-vibe/logic/query", "POST", payload)
        target = source_root / "src/sample/consumer.py"
        target.write_text(target.read_text(encoding="utf-8") + "\n# source revision changed\n", encoding="utf-8")
        with pytest.raises(urllib_error.HTTPError) as conflict:
            _json_request(base + "/v1/ap-vibe/logic/query", "POST", payload)
        assert conflict.value.code == 409
        error = json.loads(conflict.value.read().decode("utf-8"))
        assert error["error"]["code"] == "request_id_conflict"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    plain = create_server(port=0, data_dir=tmp_path / "plain", codex_project_id="project-local")
    plain_thread = threading.Thread(target=plain.serve_forever, daemon=True)
    plain_thread.start()
    plain_base = f"http://127.0.0.1:{plain.server_address[1]}"
    try:
        with pytest.raises(urllib_error.HTTPError) as unconfigured:
            _json_request(plain_base + "/v1/ap-vibe/logic/query", "POST", payload)
        assert unconfigured.value.code == 400
        error = json.loads(unconfigured.value.read().decode("utf-8"))
        assert error["error"]["code"] == "logic_field_not_configured"
        assert "逻辑观察页" in error["error"]["solution"]
        assert "Codex" in error["error"]["solution"]
    finally:
        plain.shutdown()
        plain.server_close()
        plain_thread.join(timeout=5)


def test_http_logic_contract_errors_offer_specific_user_remedies(tmp_path: Path) -> None:
    server = create_server(
        port=0,
        data_dir=tmp_path / "data",
        codex_project_id="project-local",
        logic_root=_project(tmp_path / "project"),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    cases = (
        (
            {
                "query_id": "outside-root",
                "project_id": "project-local",
                "kind": "module_responsibility",
                "target": "../outside.py",
                "source_ref": "user://logic-field/remedy",
            },
            "logic_query_target_outside_configured_root",
            "相对路径",
        ),
        (
            {
                "query_id": "short-chain",
                "project_id": "project-local",
                "kind": "first_broken_link",
                "target": "sample.core.leaf",
                "expected_path": ["sample.core.leaf"],
                "source_ref": "user://logic-field/remedy",
            },
            "first_broken_link_requires_expected_path",
            "最少",
        ),
    )
    try:
        for index, (query, code, solution_fragment) in enumerate(cases):
            with pytest.raises(urllib_error.HTTPError) as failure:
                _json_request(
                    base + "/v1/ap-vibe/logic/query",
                    "POST",
                    {"request_id": f"remedy-{index}", "query": query},
                )
            error = json.loads(failure.value.read().decode("utf-8"))["error"]
            assert error["code"] == code
            assert solution_fragment in error["solution"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
