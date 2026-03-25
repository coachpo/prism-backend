from app.routers.proxy_domains.attempt_streaming import _StreamingFinalizationBuffer


STREAMING_BUFFER_MAX_BYTES = 64 * 1024


def test_streaming_finalization_buffer_caps_partial_line_growth_for_non_audit_sse() -> (
    None
):
    buffer = _StreamingFinalizationBuffer(keep_payload=False)

    buffer.append(b"x" * (STREAMING_BUFFER_MAX_BYTES + 1))

    assert len(buffer._partial_line) <= STREAMING_BUFFER_MAX_BYTES


def test_streaming_finalization_buffer_drops_over_budget_audit_payload_and_keeps_tokens() -> (
    None
):
    buffer = _StreamingFinalizationBuffer(keep_payload=True)
    oversized_event = b"data: " + (b"x" * (STREAMING_BUFFER_MAX_BYTES + 1)) + b"\n"
    usage_event = b'data: {"usage":{"prompt_tokens":2,"completion_tokens":3}}\n\n'

    buffer.append(oversized_event)
    buffer.append(usage_event)

    payload, token_usage = buffer.finalize()

    assert payload is None
    assert token_usage == {
        "input_tokens": 2,
        "output_tokens": 3,
        "total_tokens": 5,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "reasoning_tokens": 0,
    }
