"""The retry-storm detector: same tool, transient error, no wait between attempts."""
from tracedown_server.rca.detectors import RunView, retry_storms

MS = 1_000_000  # span times are nanoseconds


def tool_span(span_id, step, start_ms, dur_ms=10, status="ERROR", message="HTTP 429 Too Many Requests",
              tool="search"):
    return {
        "span_id": span_id,
        "parent_span_id": "root",
        "kind": "tool",
        "name": tool,
        "status": {"code": status, "message": message},
        "attributes": {"tdn.step": step, "gen_ai.tool.name": tool},
        "input_digest": None,
        "output_digest": None,
        "start_time": start_ms * MS,
        "end_time": (start_ms + dur_ms) * MS,
    }


def make_run(spans):
    root = {
        "span_id": "root",
        "parent_span_id": None,
        "kind": "agent",
        "name": "agent",
        "status": {"code": "OK", "message": ""},
        "attributes": {"tdn.step": 0},
        "input_digest": None,
        "output_digest": None,
        "start_time": 0,
        "end_time": 0,
    }
    return RunView(spans=[root] + spans, blobs={})


def storm_spans(n=5, gap_ms=5, **kw):
    spans, t = [], 100
    for i in range(n):
        spans.append(tool_span(f"s{i}", i + 1, t, **kw))
        t += 10 + gap_ms
    return spans


def test_five_fast_429s_are_a_retry_storm():
    findings = retry_storms(make_run(storm_spans()))
    assert len(findings) == 1
    f = findings[0]
    assert f.category == "retry_storm"
    assert f.step == 1
    assert f.span_id == "s0"
    assert any("5 calls to search" in e for e in f.evidence)
    assert any("5 ms" in e for e in f.evidence)


def test_one_retry_after_a_wait_that_succeeds_stays_quiet():
    spans = [
        tool_span("s0", 1, 100, message="503 service unavailable"),
        tool_span("s1", 2, 100 + 10 + 5000, status="OK", message=""),
    ]
    assert retry_storms(make_run(spans)) == []


def test_two_immediate_failures_are_not_a_storm_yet():
    assert retry_storms(make_run(storm_spans(n=2))) == []


def test_failures_with_real_waits_between_them_stay_quiet():
    assert retry_storms(make_run(storm_spans(n=4, gap_ms=2000))) == []


def test_non_transient_errors_are_not_a_storm():
    spans = storm_spans(n=4, message="order 123 not found")
    assert retry_storms(make_run(spans)) == []


def test_a_wait_splits_two_streaks_and_only_the_storm_is_reported():
    spans = storm_spans(n=2)  # two fast failures, then a long pause
    t = 100 + 2 * 15 + 60_000
    for i in range(3):
        spans.append(tool_span(f"t{i}", 3 + i, t))
        t += 15
    findings = retry_storms(make_run(spans))
    assert len(findings) == 1
    assert findings[0].step == 3


def test_failures_spread_across_tools_do_not_add_up():
    spans = []
    t = 100
    for i, tool in enumerate(["search", "fetch_page", "search", "fetch_page"]):
        spans.append(tool_span(f"s{i}", i + 1, t, tool=tool))
        t += 15
    assert retry_storms(make_run(spans)) == []


def test_timeouts_and_503s_count_as_transient():
    spans = storm_spans(n=3, message="request timed out")
    assert len(retry_storms(make_run(spans))) == 1
    spans = storm_spans(n=3, message="503 Service Unavailable")
    assert len(retry_storms(make_run(spans))) == 1
