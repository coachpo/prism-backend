import json

def _parse_sse_events(raw: bytes) -> list[dict]:
    events = []
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return events
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("data: ") and line != "data: [DONE]":
            try:
                events.append(json.loads(line[6:]))
            except (json.JSONDecodeError, ValueError):
                continue
    return events

def _empty_usage() -> dict[str, int | None]:
    return {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
        "reasoning_tokens": None,
    }

def _as_int(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def _pick_int(*values) -> int | None:
    for value in values:
        parsed = _as_int(value)
        if parsed is not None:
            return parsed
    return None

def _extract_special_usage(
    usage: dict,
) -> tuple[int | None, int | None, int | None]:
    prompt_details = (
        usage.get("prompt_tokens_details")
        or usage.get("input_tokens_details")
    )
    completion_details = (
        usage.get("completion_tokens_details")
        or usage.get("output_tokens_details")
    )

    cache_read_input_tokens = None
    cache_creation_input_tokens = None
    reasoning_tokens = None

    if isinstance(prompt_details, dict):
        cache_read_input_tokens = _pick_int(
            prompt_details.get("cached_tokens"),
            prompt_details.get("cache_read_input_tokens"),
            prompt_details.get("cached_input_tokens"),
            prompt_details.get("cachedContentTokenCount"),
        )
        cache_creation_input_tokens = _pick_int(
            prompt_details.get("cache_creation_input_tokens"),
            prompt_details.get("cache_creation_tokens"),
            prompt_details.get("cacheCreationInputTokens"),
            prompt_details.get("cacheCreationTokens"),
        )

    if isinstance(completion_details, dict):
        reasoning_tokens = _pick_int(
            completion_details.get("reasoning_tokens"),
            completion_details.get("reasoningTokenCount"),
        )

    if cache_read_input_tokens is None:
        cache_read_input_tokens = _pick_int(
            usage.get("cache_read_input_tokens"),
            usage.get("cached_input_tokens"),
            usage.get("cachedContentTokenCount"),
        )
    if cache_creation_input_tokens is None:
        cache_creation_input_tokens = _pick_int(
            usage.get("cache_creation_input_tokens"),
            usage.get("cache_creation_tokens"),
            usage.get("cacheCreationInputTokens"),
            usage.get("cacheCreationTokens"),
        )
    if reasoning_tokens is None:
        reasoning_tokens = _pick_int(
            usage.get("reasoning_tokens"),
            usage.get("reasoningTokenCount"),
        )

    return (
        cache_read_input_tokens,
        cache_creation_input_tokens,
        reasoning_tokens,
    )

def _extract_from_sse(raw: bytes) -> dict[str, int | None]:
    events = _parse_sse_events(raw)
    if not events:
        return _empty_usage()

    input_tokens = None
    output_tokens = None
    total_tokens = None
    cache_read_input_tokens = None
    cache_creation_input_tokens = None
    reasoning_tokens = None
    usage_seen = False

    for event in events:
        usage = event.get("usage")
        if usage is None:
            response_payload = event.get("response")
            if isinstance(response_payload, dict):
                nested_usage = response_payload.get("usage")
                if isinstance(nested_usage, dict):
                    usage = nested_usage

        if isinstance(usage, dict):
            usage_seen = True
            input_tokens = _pick_int(
                usage.get("prompt_tokens"),
                usage.get("input_tokens"),
                input_tokens,
            )
            output_tokens = _pick_int(
                usage.get("completion_tokens"),
                usage.get("output_tokens"),
                output_tokens,
            )
            total_tokens = _pick_int(usage.get("total_tokens"), total_tokens)
            cached_found, cache_creation_found, reasoning_found = (
                _extract_special_usage(usage)
            )
            cache_read_input_tokens = _pick_int(cached_found, cache_read_input_tokens)
            cache_creation_input_tokens = _pick_int(
                cache_creation_found,
                cache_creation_input_tokens,
            )
            reasoning_tokens = _pick_int(reasoning_found, reasoning_tokens)

        if event.get("type") == "message_start":
            msg_usage = event.get("message", {}).get("usage", {})
            if isinstance(msg_usage, dict):
                usage_seen = True
                if msg_usage.get("input_tokens") is not None:
                    input_tokens = _pick_int(
                        msg_usage.get("input_tokens"), input_tokens
                    )
                cached_found, cache_creation_found, reasoning_found = (
                    _extract_special_usage(msg_usage)
                )
                cache_read_input_tokens = _pick_int(
                    cached_found,
                    cache_read_input_tokens,
                )
                cache_creation_input_tokens = _pick_int(
                    cache_creation_found,
                    cache_creation_input_tokens,
                )
                reasoning_tokens = _pick_int(reasoning_found, reasoning_tokens)

        if event.get("type") == "message_delta":
            delta_usage = event.get("usage", {})
            if isinstance(delta_usage, dict):
                usage_seen = True
                if delta_usage.get("output_tokens") is not None:
                    output_tokens = _pick_int(
                        delta_usage.get("output_tokens"), output_tokens
                    )
                cached_found, cache_creation_found, reasoning_found = (
                    _extract_special_usage(delta_usage)
                )
                cache_read_input_tokens = _pick_int(
                    cached_found,
                    cache_read_input_tokens,
                )
                cache_creation_input_tokens = _pick_int(
                    cache_creation_found,
                    cache_creation_input_tokens,
                )
                reasoning_tokens = _pick_int(reasoning_found, reasoning_tokens)

        gemini_usage = event.get("usageMetadata")
        if gemini_usage and isinstance(gemini_usage, dict):
            usage_seen = True
            input_tokens = _pick_int(
                gemini_usage.get("promptTokenCount"),
                input_tokens,
            )
            output_tokens = _pick_int(
                gemini_usage.get("candidatesTokenCount"),
                output_tokens,
            )
            total_tokens = _pick_int(
                gemini_usage.get("totalTokenCount"),
                total_tokens,
            )
            cache_read_input_tokens = _pick_int(
                gemini_usage.get("cachedContentTokenCount"),
                cache_read_input_tokens,
            )
            reasoning_tokens = _pick_int(
                gemini_usage.get("thoughtsTokenCount"),
                reasoning_tokens,
            )

    if total_tokens is None and (input_tokens is not None or output_tokens is not None):
        total_tokens = (input_tokens or 0) + (output_tokens or 0)

    if usage_seen:
        cache_read_input_tokens = (
            cache_read_input_tokens if cache_read_input_tokens is not None else 0
        )
        cache_creation_input_tokens = (
            cache_creation_input_tokens
            if cache_creation_input_tokens is not None
            else 0
        )
        reasoning_tokens = reasoning_tokens if reasoning_tokens is not None else 0

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "reasoning_tokens": reasoning_tokens,
    }

def extract_token_usage(body: bytes | None) -> dict[str, int | None]:
    if not body:
        return _empty_usage()

    try:
        text_preview = body[:100].decode("utf-8", errors="replace")
    except Exception:
        text_preview = ""
    if "data: " in text_preview:
        return _extract_from_sse(body)

    try:
        data = json.loads(body)
        usage = data.get("usage")
        if isinstance(usage, dict):
            input_t = _pick_int(usage.get("prompt_tokens"), usage.get("input_tokens"))
            output_t = _pick_int(
                usage.get("completion_tokens"), usage.get("output_tokens")
            )
            total_t = _pick_int(usage.get("total_tokens"))
            (
                cache_read_input_tokens,
                cache_creation_input_tokens,
                reasoning_tokens,
            ) = _extract_special_usage(usage)
            cache_read_input_tokens = (
                cache_read_input_tokens if cache_read_input_tokens is not None else 0
            )
            cache_creation_input_tokens = (
                cache_creation_input_tokens
                if cache_creation_input_tokens is not None
                else 0
            )
            reasoning_tokens = reasoning_tokens if reasoning_tokens is not None else 0
            if total_t is None and (input_t is not None or output_t is not None):
                total_t = (input_t or 0) + (output_t or 0)
            return {
                "input_tokens": input_t,
                "output_tokens": output_t,
                "total_tokens": total_t,
                "cache_read_input_tokens": cache_read_input_tokens,
                "cache_creation_input_tokens": cache_creation_input_tokens,
                "reasoning_tokens": reasoning_tokens,
            }

        gemini_usage = data.get("usageMetadata")
        if gemini_usage and isinstance(gemini_usage, dict):
            input_t = _pick_int(gemini_usage.get("promptTokenCount"))
            output_t = _pick_int(gemini_usage.get("candidatesTokenCount"))
            total_t = _pick_int(gemini_usage.get("totalTokenCount"))
            cache_read_input_tokens = _pick_int(
                gemini_usage.get("cachedContentTokenCount")
            )
            reasoning_tokens = _pick_int(gemini_usage.get("thoughtsTokenCount"))
            cache_read_input_tokens = (
                cache_read_input_tokens if cache_read_input_tokens is not None else 0
            )
            cache_creation_input_tokens = 0
            reasoning_tokens = reasoning_tokens if reasoning_tokens is not None else 0
            if total_t is None and (input_t is not None or output_t is not None):
                total_t = (input_t or 0) + (output_t or 0)
            return {
                "input_tokens": input_t,
                "output_tokens": output_t,
                "total_tokens": total_t,
                "cache_read_input_tokens": cache_read_input_tokens,
                "cache_creation_input_tokens": cache_creation_input_tokens,
                "reasoning_tokens": reasoning_tokens,
            }

        if "input_tokens" in data and "usage" not in data:
            return {
                "input_tokens": _pick_int(data.get("input_tokens")),
                "output_tokens": None,
                "total_tokens": None,
                "cache_read_input_tokens": None,
                "cache_creation_input_tokens": None,
                "reasoning_tokens": None,
            }

        return _empty_usage()
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return _empty_usage()
