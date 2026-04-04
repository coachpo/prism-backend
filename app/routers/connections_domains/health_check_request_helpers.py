import time

import httpx

def _extract_upstream_error_message(response: httpx.Response) -> str:
    if response.status_code < 400:
        return ""
    try:
        response_json = response.json()
    except Exception:
        return ""
    if not isinstance(response_json, dict):
        return ""

    error = response_json.get("error", {})
    if isinstance(error, dict):
        message = error.get("message", "")
        return message if isinstance(message, str) else str(message)
    if isinstance(error, str):
        return error
    return ""


def _map_health_check_response(response: httpx.Response) -> tuple[str, str]:
    upstream_msg = _extract_upstream_error_message(response)
    if 200 <= response.status_code < 300:
        return "healthy", "Connection successful"
    if response.status_code == 429:
        return "healthy", "Rate limited (connection works)"
    if response.status_code in (401, 403):
        detail = f"Authentication failed (HTTP {response.status_code})"
        if upstream_msg:
            detail += f": {upstream_msg}"
        return "unhealthy", detail

    detail = f"HTTP {response.status_code}"
    if upstream_msg:
        detail += f": {upstream_msg}"
    return "unhealthy", detail


async def _execute_health_check_request(
    client: httpx.AsyncClient,
    *,
    upstream_url: str,
    headers: dict[str, str],
    body: dict[str, object],
) -> tuple[str, str, int]:
    try:
        start = time.monotonic()
        response = await client.post(
            upstream_url,
            headers=headers,
            json=body,
            timeout=30.0,
        )
        response_time_ms = int((time.monotonic() - start) * 1000)
        health_status, detail = _map_health_check_response(response)
        return health_status, detail, response_time_ms
    except httpx.ConnectError as exc:
        return "unhealthy", f"Connection failed: {exc}", 0
    except httpx.TimeoutException:
        return "unhealthy", "Connection timed out", 0
    except Exception as exc:
        return "unhealthy", f"Error: {exc}", 0
