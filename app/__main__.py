from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Sequence

import uvicorn


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default

    try:
        return int(value)
    except ValueError:
        return default


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Prism backend server.")
    parser.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=_int_env("PORT", 8000))
    parser.add_argument(
        "--workers",
        type=int,
        default=_int_env("PRISM_BACKEND_WORKERS", 4),
    )
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "info"))
    parser.add_argument(
        "--forwarded-allow-ips",
        default=os.getenv("FORWARDED_ALLOW_IPS", "*"),
    )
    parser.add_argument(
        "--no-proxy-headers",
        action="store_false",
        dest="proxy_headers",
    )
    parser.set_defaults(proxy_headers=True)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    workers = 1 if args.reload else args.workers
    if workers > 1:
        from app.bootstrap.startup import (
            SKIP_STARTUP_SEQUENCE_ENV,
            run_startup_sequence,
        )

        asyncio.run(run_startup_sequence())
        os.environ[SKIP_STARTUP_SEQUENCE_ENV] = "1"

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        workers=workers,
        reload=args.reload,
        log_level=args.log_level,
        proxy_headers=args.proxy_headers,
        forwarded_allow_ips=args.forwarded_allow_ips,
    )


if __name__ == "__main__":
    main()
