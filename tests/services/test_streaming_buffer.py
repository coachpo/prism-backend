from app.routers.proxy_domains.attempt_streaming import (
    TRUNCATED_SSE_SENTINEL,
    _StreamingFinalizationBuffer,
)


STREAMING_BUFFER_MAX_BYTES = 64 * 1024


def test_streaming_finalization_buffer_caps_partial_line_growth_for_non_audit_sse() -> (
    None
):
    buffer = _StreamingFinalizationBuffer(keep_payload=False)

    buffer.append(b"x" * (STREAMING_BUFFER_MAX_BYTES + 1))

    assert len(buffer._partial_line) <= STREAMING_BUFFER_MAX_BYTES


def test_streaming_finalization_buffer_truncates_over_budget_audit_payload_and_keeps_tokens() -> (
    None
):
    buffer = _StreamingFinalizationBuffer(keep_payload=True)
    oversized_event = b"data: " + (b"x" * (STREAMING_BUFFER_MAX_BYTES + 1)) + b"\n"
    usage_event = b'data: {"usage":{"prompt_tokens":2,"completion_tokens":3}}\n\n'

    buffer.append(oversized_event)
    buffer.append(usage_event)

    payload, token_usage, provider_correlation_id = buffer.finalize()

    assert payload is not None
    assert len(payload) <= STREAMING_BUFFER_MAX_BYTES
    assert TRUNCATED_SSE_SENTINEL in payload
    assert provider_correlation_id is None
    assert token_usage == {
        "input_tokens": 2,
        "output_tokens": 3,
        "total_tokens": 5,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "reasoning_tokens": 0,
    }


def test_streaming_finalization_buffer_parses_openai_response_completed_usage_without_payload() -> (
    None
):
    buffer = _StreamingFinalizationBuffer(keep_payload=False)
    completed_event = (
        b"event: response.completed\n"
        b'data: {"type":"response.completed","response":{"id":"resp_123","usage":{"input_tokens":75,"output_tokens":125,"total_tokens":200,"input_tokens_details":{"cached_tokens":32},"output_tokens_details":{"reasoning_tokens":64}}}}\n\n'
    )

    for offset in range(0, len(completed_event), 7):
        buffer.append(completed_event[offset : offset + 7])

    payload, token_usage, provider_correlation_id = buffer.finalize()

    assert payload is None
    assert provider_correlation_id is None
    assert token_usage == {
        "input_tokens": 75,
        "output_tokens": 125,
        "total_tokens": 200,
        "cache_read_input_tokens": 32,
        "cache_creation_input_tokens": 0,
        "reasoning_tokens": 64,
    }


def test_streaming_finalization_buffer_preserves_usage_from_oversized_response_completed_event() -> (
    None
):
    buffer = _StreamingFinalizationBuffer(keep_payload=False)
    large_text = "A" * 70_000
    completed_event = b"event: response.completed\n" + (
        'data: {"type":"response.completed","response":{"id":"resp_123","output":[{"type":"message","id":"msg_123","status":"completed","role":"assistant","content":[{"type":"output_text","text":"'
        + large_text
        + '"}]}],"usage":{"input_tokens":75,"output_tokens":125,"total_tokens":200,"input_tokens_details":{"cached_tokens":32},"output_tokens_details":{"reasoning_tokens":64}}}}\n\n'
    ).encode("utf-8")

    for offset in range(0, len(completed_event), 4096):
        buffer.append(completed_event[offset : offset + 4096])

    payload, token_usage, provider_correlation_id = buffer.finalize()

    assert payload is None
    assert provider_correlation_id is None
    assert token_usage == {
        "input_tokens": 75,
        "output_tokens": 125,
        "total_tokens": 200,
        "cache_read_input_tokens": 32,
        "cache_creation_input_tokens": 0,
        "reasoning_tokens": 64,
    }


def test_streaming_finalization_buffer_preserves_usage_from_oversized_response_completed_event_with_large_suffix() -> (
    None
):
    buffer = _StreamingFinalizationBuffer(keep_payload=False)
    large_prefix = "A" * 40_000
    large_suffix = "B" * 70_000
    completed_event = b"event: response.completed\n" + (
        'data: {"type":"response.completed","response":{"id":"resp_123","output":[{"type":"message","id":"msg_123","status":"completed","role":"assistant","content":[{"type":"output_text","text":"'
        + large_prefix
        + '"}]}],"usage":{"input_tokens":75,"output_tokens":125,"total_tokens":200,"input_tokens_details":{"cached_tokens":32},"output_tokens_details":{"reasoning_tokens":64}},"trace":"'
        + large_suffix
        + '"}}\n\n'
    ).encode("utf-8")

    for offset in range(0, len(completed_event), 4096):
        buffer.append(completed_event[offset : offset + 4096])

    payload, token_usage, provider_correlation_id = buffer.finalize()

    assert payload is None
    assert provider_correlation_id is None
    assert token_usage == {
        "input_tokens": 75,
        "output_tokens": 125,
        "total_tokens": 200,
        "cache_read_input_tokens": 32,
        "cache_creation_input_tokens": 0,
        "reasoning_tokens": 64,
    }
