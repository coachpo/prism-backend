import json


def extract_model_from_body(raw_body: bytes) -> str | None:
    try:
        parsed = json.loads(raw_body)
        return parsed.get("model")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def extract_stream_flag(raw_body: bytes) -> bool:
    try:
        parsed = json.loads(raw_body)
        return bool(parsed.get("stream", False))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False


__all__ = ["extract_model_from_body", "extract_stream_flag"]
