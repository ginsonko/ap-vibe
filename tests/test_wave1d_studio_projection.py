from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ap_mind import StudioEpisodeRequest, run_studio_episode


def test_studio_episode_projects_real_revision_chain(tmp_path: Path) -> None:
    view = run_studio_episode(
        tmp_path / "studio.sqlite",
        StudioEpisodeRequest(
            request_id="studio-revision",
            proposition_text="黄色苹果",
            surface_text="黄色苹蕉",
        ),
    )
    data = view.to_dict()
    assert data["status"] == "success"
    assert data["physical_output"] == "黄色苹果"
    assert data["counters"] == {
        "cognitive_ticks": 6,
        "provider_waits": 0,
        "output_units": 4,
        "tool_progress": 0,
        "readbacks": 5,
    }
    revision = next(
        tick for tick in data["ticks"] if tick["decision"]["selected_kind"] == "revise_expression"
    )
    assert revision["decision"]["revision_review"]["eligible"] is True
    assert any(item["name"] == "incongruity" for item in revision["feelings"])
    assert revision["output"]["unit"] is None
    assert revision["output"]["draft"]["units"] == list("黄色苹果")
    assert len(data["causal_summary"]) <= 7
    assert data["ownership"]["tick_and_action_arena"] == "native"
    assert data["ownership"]["surface_renderer"] == "assisted_fixture"
    assert data["product_effect"] == "local_runtime_episode"


def test_studio_native_surface_has_no_revision(tmp_path: Path) -> None:
    view = run_studio_episode(
        tmp_path / "studio-native.sqlite",
        StudioEpisodeRequest(
            request_id="studio-native",
            proposition_text="你好",
            surface_text=None,
        ),
    )
    assert view.status == "success"
    assert view.physical_output == "你好"
    assert view.counters["cognitive_ticks"] == 3
    assert view.counters["output_units"] == 2
    assert view.counters["readbacks"] == 2
    assert not any(
        tick["decision"]["selected_kind"] == "revise_expression" for tick in view.ticks
    )
    assert view.ownership["surface_renderer"] == "native"


def test_studio_annotation_off_is_honest_partial_not_fake_correction(tmp_path: Path) -> None:
    view = run_studio_episode(
        tmp_path / "studio-off.sqlite",
        StudioEpisodeRequest(
            request_id="studio-off",
            proposition_text="甲丙",
            surface_text="甲乙",
            annotation_enabled=False,
        ),
    )
    assert view.status == "partial"
    assert view.physical_output == "甲乙"
    assert "readback_annotation_disabled_surface_not_corrected" in view.limitations
    assert "physical_output_differs_from_canonical_proposition" in view.limitations
    assert view.ownership["readback_annotation"] == "absent"


def test_studio_same_request_and_database_replays_without_duplicate_output(tmp_path: Path) -> None:
    database = tmp_path / "studio-replay.sqlite"
    request = StudioEpisodeRequest(
        request_id="studio-replay",
        proposition_text="甲丙",
        surface_text="甲乙",
    )
    first = run_studio_episode(database, request)
    second = run_studio_episode(database, request)
    assert second.to_dict() == first.to_dict()
    assert second.physical_output == "甲丙"
