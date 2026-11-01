import pytest
from tracedown_server.rca.detectors import RunView, tool_errors

SUCCESS_PHRASES = [
    "The refund was issued.",
    "Your booking is complete.",
    "You're all set.",
    "We've cancelled it.",
    "It's on its way.",
    "Done!",
]


def make_run(
    answer: str, tool_name: str = "refund_payment", tool_status: str = "ERROR"
) -> RunView:
    spans = [
        {
            "span_id": "root",
            "parent_span_id": None,
            "kind": "agent",
            "name": "agent",
            "status": {"code": "OK", "message": ""},
            "attributes": {"tdn.step": 0},
            "input_digest": None,
            "output_digest": "answer",
        },
        {
            "span_id": "write",
            "parent_span_id": "root",
            "kind": "tool",
            "name": tool_name,
            "status": {"code": tool_status, "message": "503 service unavailable"},
            "attributes": {"tdn.step": 1, "gen_ai.tool.name": tool_name},
            "input_digest": "args",
            "output_digest": None,
        },
        {
            "span_id": "final",
            "parent_span_id": "root",
            "kind": "llm",
            "name": "assistant",
            "status": {"code": "OK", "message": ""},
            "attributes": {"tdn.step": 2},
            "input_digest": None,
            "output_digest": None,
        },
    ]
    return RunView(spans=spans, blobs={"answer": answer, "args": '{"id": "123"}'})


@pytest.mark.parametrize("answer", SUCCESS_PHRASES)
def test_reports_new_success_phrases_after_failed_write(answer: str) -> None:
    findings = tool_errors(make_run(answer))

    assert len(findings) == 1
    assert any(
        "final answer still reports success" in item for item in findings[0].evidence
    )


def test_successful_write_does_not_report_a_false_success_claim() -> None:
    assert tool_errors(make_run("Done!", tool_status="OK")) == []


def test_failed_lookup_does_not_report_a_success_claim() -> None:
    findings = tool_errors(make_run("Done!", tool_name="get_order"))

    assert len(findings) == 1
    assert not any(
        "final answer still reports success" in item for item in findings[0].evidence
    )
