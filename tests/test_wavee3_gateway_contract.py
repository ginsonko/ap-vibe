from __future__ import annotations

import json

from ap_mind.gateway import GatewayCallReceipt, OpenAICompatibleGateway


class E3TeacherTransport:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def request_json(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        payload = {
            "status": "proposal",
            # This capability was deliberately not requested below.  Keeping
            # it in the response checks that the input fence, not the model's
            # compliance, controls authority.
            "recall_candidates": [
                {
                    "memory_ref": "evt-memory",
                    "summary": "不应进入本次 recall 教学",
                    "relevance": 0.8,
                    "rationale": "provider returned an unrequested family",
                }
            ],
            "paradigm_candidates": [
                {
                    "pattern_kind": "relation_frame",
                    "invariants": {
                        "source_completeness": "partial",
                        "has_open_items": True,
                    },
                    "slots": [
                        {"name": "claim", "source": "proposition.content", "required": True},
                        {"name": "next_action", "source": "activity.observed_next_action", "required": False},
                    ],
                    "relations": [
                        {"source_slot": "claim", "target_slot": "next_action", "relation": "precedes"}
                    ],
                    "confidence": 0.78,
                    "uncertainty": 0.22,
                    "evidence_refs": ["evt-current"],
                    "completeness": "partial",
                    "counterexamples": ["没有未闭合事项时不应套用"],
                },
                {
                    "pattern_kind": "relation_frame",
                    "invariants": {"event_kind": "progress"},
                    "slots": [
                        {"name": "answer", "source": "activity.answer", "required": True}
                    ],
                    "relations": [],
                    "confidence": 0.9,
                    "uncertainty": 0.1,
                    "evidence_refs": ["invented-ref"],
                    "completeness": "complete",
                },
            ],
            "attention_candidates": [
                {
                    "mode": "shift_attention",
                    "target_ref": "evt-memory",
                    "gain_delta": 0.35,
                    "rationale": "最近相关经历可能补足未闭合信息",
                    "source_refs": ["evt-current", "evt-memory"],
                    "uncertainty": 0.25,
                },
                {
                    "mode": "shift_attention",
                    "target_ref": "invented-focus",
                    "gain_delta": 4.0,
                    "rationale": "invalid fixture",
                    "source_refs": ["invented-ref"],
                    "uncertainty": 0.1,
                },
            ],
            "expression_candidates": [
                {
                    "template": "我目前能确认的是：{claim}",
                    "tone": "careful",
                    "evidence_refs": ["prop-current"],
                    "uncertainty": 0.15,
                    "counterexamples": ["无需强调证据边界时保留原表达"],
                },
                {
                    "template": "{claim}，并且工具已经成功执行",
                    "tone": "warm",
                    "evidence_refs": ["prop-current"],
                    "uncertainty": 0.05,
                },
                {
                    "template": "{claim}，因此{conclusion}",
                    "tone": "neutral",
                    "evidence_refs": ["prop-current"],
                    "uncertainty": 0.2,
                },
            ],
            "parameter_candidates": [
                {
                    "parameter": "attention.novelty_weight",
                    "delta": 0.04,
                    "rationale": "新信息在当前证据结构中应获得稍高注意增益",
                    "source_refs": ["evt-current"],
                    "uncertainty": 0.2,
                    "counterexamples": ["该方向未改善后续注意时撤回"],
                    "scope": {"source_completeness": "partial", "has_open_items": True},
                    "expected_direction": "increase",
                },
                {
                    "parameter": "attention.winner_override",
                    "delta": 0.8,
                    "rationale": "invalid hidden winner",
                    "source_refs": ["invented-ref"],
                    "uncertainty": 0.0,
                    "scope": {"event_kind": "progress"},
                    "expected_direction": "increase",
                },
            ],
            "prediction_candidates": [],
            "appraisal_candidates": [],
            "thought_candidates": [],
            "candidate_preferences": {},
            "selected_candidate_ref": None,
            "lesson_candidates": [],
            "uncertainty": 0.25,
            "limitations": ["teacher_candidates_are_not_reality"],
        }
        return {
            "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 120, "total_tokens": 220},
        }


def _frame() -> dict:
    return {
        "sa": {"event_ref": "evt-current", "occurrence_id": "sa-current"},
        "b_recall": [{"ref": "evt-memory", "event_ref": "evt-memory", "summary": "此前仍有一项待办"}],
        "c_prediction": [],
        "feelings": [],
        "proposition": {
            "proposition_id": "prop-current",
            "content": "浏览器回读仍未完成",
            "source_refs": ["evt-current"],
        },
        "expression_draft": {
            "draft_id": "draft-current",
            "proposition_ref": "prop-current",
            "units": ["浏览器回读仍未完成"],
        },
        "activity": {
            "summary": "实现已结束但浏览器回读未完成",
            "observed_next_action": "读取浏览器结果",
            "observed_remaining": ["浏览器回读"],
            "observed_unknown": [],
        },
        "activity_profile": {
            "source_completeness": "partial",
            "has_open_items": True,
            "has_unknowns": False,
            "has_conflicts": False,
            "has_next_action": True,
        },
        "action_candidates": [{"candidate_id": "act-existing"}],
        "requested_capabilities": [
            "project.paradigm",
            "project.attention",
            "project.expression",
            "project.parameter_tuning",
        ],
    }


def test_e3_gateway_candidates_are_bounded_requested_and_recoverable() -> None:
    transport = E3TeacherTransport()
    synthetic_key = "s" + "k-contract-secret-never-persist"
    gateway = OpenAICompatibleGateway(
        "https://provider.invalid/v1",
        synthetic_key,
        "gpt-test",
        transport=transport,
        max_retries=0,
    )

    proposal = gateway.propose(_frame(), None)

    assert proposal.prompt_template_version == "apv4.gateway.v4"
    assert proposal.requested_capabilities == (
        "project.paradigm",
        "project.attention",
        "project.expression",
        "project.parameter_tuning",
    )
    assert proposal.recall_candidates[0]["validation"] == "rejected"
    assert "capability_not_requested" in proposal.recall_candidates[0]["rejection_reasons"]

    assert proposal.paradigm_candidates[0]["validation"] == "accepted"
    assert proposal.paradigm_candidates[0]["slots"][0] == {
        "name": "claim",
        "source": "proposition.content",
        "required": True,
    }
    assert proposal.paradigm_candidates[1]["validation"] == "rejected"
    assert any(
        reason.startswith(("unsupported_invariant", "unsupported_slot_source"))
        for reason in proposal.paradigm_candidates[1]["rejection_reasons"]
    )

    assert proposal.attention_candidates[0]["validation"] == "accepted"
    assert proposal.attention_candidates[0]["target_ref"] == "evt-memory"
    assert proposal.attention_candidates[1]["validation"] == "rejected"
    assert "unobserved_attention_target_ref" in proposal.attention_candidates[1]["rejection_reasons"]
    assert "attention_gain_out_of_bounds" in proposal.attention_candidates[1]["rejection_reasons"]

    assert proposal.expression_candidates[0]["validation"] == "accepted"
    assert proposal.expression_candidates[0]["prefix"] == "我目前能确认的是："
    assert proposal.expression_candidates[1]["validation"] == "rejected"
    assert "expression_suffix_not_pragmatic" in proposal.expression_candidates[1]["rejection_reasons"]
    assert proposal.expression_candidates[2]["validation"] == "rejected"
    assert "expression_contains_unsupported_slot" in proposal.expression_candidates[2]["rejection_reasons"]
    assert proposal.parameter_candidates[0]["validation"] == "accepted"
    assert proposal.parameter_candidates[0]["delta"] == 0.04
    assert proposal.parameter_candidates[0]["bounds"] == [0.05, 0.45]
    assert proposal.parameter_candidates[1]["validation"] == "rejected"
    assert "unsupported_parameter" in proposal.parameter_candidates[1]["rejection_reasons"]
    assert "parameter_delta_out_of_bounds" in proposal.parameter_candidates[1]["rejection_reasons"]
    assert "unsupported_parameter_scope" in proposal.parameter_candidates[1]["rejection_reasons"]

    receipt = GatewayCallReceipt.from_proposal(
        proposal,
        capability_key="hybrid.cognition",
        input_refs=("evt-current",),
    )
    assert receipt is not None
    restored = GatewayCallReceipt.from_dict(receipt.to_dict()).to_proposal()
    assert restored is not None
    assert restored.paradigm_candidates == proposal.paradigm_candidates
    assert restored.attention_candidates == proposal.attention_candidates
    assert restored.expression_candidates == proposal.expression_candidates
    assert restored.parameter_candidates == proposal.parameter_candidates
    encoded = json.dumps(receipt.to_dict(), ensure_ascii=False)
    assert "contract-secret-never-persist" not in encoded

    outbound = json.loads(transport.calls[0]["body"]["messages"][1]["content"])
    assert outbound["frame_view"]["requested_capabilities"] == [
        "project.paradigm",
        "project.attention",
        "project.expression",
        "project.parameter_tuning",
    ]


def test_explicit_empty_capability_set_does_not_expand_to_all() -> None:
    transport = E3TeacherTransport()
    gateway = OpenAICompatibleGateway(
        "https://provider.invalid/v1",
        "not-persisted",
        "gpt-test",
        transport=transport,
        max_retries=0,
    )
    frame = _frame()
    frame["requested_capabilities"] = []

    proposal = gateway.propose(frame, None)

    assert proposal.requested_capabilities == ()
    assert all(item["validation"] == "rejected" for item in proposal.paradigm_candidates)
    assert all(item["validation"] == "rejected" for item in proposal.attention_candidates)
    assert all(item["validation"] == "rejected" for item in proposal.expression_candidates)
    assert all(item["validation"] == "rejected" for item in proposal.parameter_candidates)
