from collections.abc import AsyncGenerator, Sequence

import httpx


async def proxy_request(
    client: httpx.AsyncClient,
    method: str,
    upstream_url: str,
    headers: dict[str, str],
    raw_body: bytes | None,
) -> httpx.Response:
    if raw_body is None:
        send_req = client.build_request(method, upstream_url, headers=headers)
    else:
        send_req = client.build_request(
            method,
            upstream_url,
            headers=headers,
            content=raw_body,
        )
    return await client.send(send_req, follow_redirects=True)


async def proxy_stream(
    client: httpx.AsyncClient,
    method: str,
    upstream_url: str,
    headers: dict[str, str],
    raw_body: bytes | None,
) -> AsyncGenerator[tuple[bytes, httpx.Headers, int], None]:
    if raw_body is None:
        stream_context = client.stream(method, upstream_url, headers=headers)
    else:
        stream_context = client.stream(
            method,
            upstream_url,
            headers=headers,
            content=raw_body,
        )
    async with stream_context as response:
        if response.status_code >= 400:
            _ = await response.aread()
            yield response.content, response.headers, response.status_code
            return
        async for chunk in response.aiter_bytes():
            if chunk:
                yield chunk, response.headers, response.status_code


def should_failover(status_code: int, failover_status_codes: Sequence[int]) -> bool:
    return status_code in failover_status_codes


__all__ = ["proxy_request", "proxy_stream", "should_failover"]
