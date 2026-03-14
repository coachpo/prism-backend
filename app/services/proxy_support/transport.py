from typing import AsyncGenerator

import httpx

from .constants import FAILOVER_STATUS_CODES


async def proxy_request(
    client: httpx.AsyncClient,
    method: str,
    upstream_url: str,
    headers: dict[str, str],
    raw_body: bytes | None,
) -> httpx.Response:
    kwargs: dict = {"headers": headers}
    if raw_body:
        kwargs["content"] = raw_body
    send_req = client.build_request(method, upstream_url, **kwargs)
    return await client.send(send_req, follow_redirects=True)


async def proxy_stream(
    client: httpx.AsyncClient,
    method: str,
    upstream_url: str,
    headers: dict[str, str],
    raw_body: bytes | None,
) -> AsyncGenerator[tuple[bytes, httpx.Headers, int], None]:
    kwargs: dict = {"headers": headers}
    if raw_body:
        kwargs["content"] = raw_body
    async with client.stream(method, upstream_url, **kwargs) as response:
        if response.status_code >= 400:
            await response.aread()
            yield response.content, response.headers, response.status_code
            return
        async for chunk in response.aiter_bytes():
            if chunk:
                yield chunk, response.headers, response.status_code


def should_failover(status_code: int) -> bool:
    return status_code in FAILOVER_STATUS_CODES


__all__ = ["proxy_request", "proxy_stream", "should_failover"]
