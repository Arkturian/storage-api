#!/usr/bin/env python3
"""
Warm up Storage API media derivatives for all assets of a tenant.

Fetches every object via /storage/list and triggers the media endpoint twice:
 - width=130  (thumbnail)
 - width=1300 (hero)

Each request is made with refresh=true so the Storage API regenerates files even
if they already exist. The script is network-heavy but idempotent.

Usage:
    python scripts/warmup_media.py --tenant oneal \
        --api-base https://api-storage.arkturian.com \
        --api-key <api-key>
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass
from typing import AsyncIterator, Iterable, List

import httpx


@dataclass
class WarmupConfig:
    api_base: str
    api_key: str
    tenant: str
    page_size: int = 500
    concurrency: int = 8
    request_timeout: float = 180.0
    connect_timeout: float = 30.0
    retry_limit: int = 3
    sleep_between_pages: float = 0.5  # seconds
    sleep_between_retries: float = 1.5
    widths: Iterable[int] = (130, 1300)
    format: str = "webp"
    quality_small: int = 75
    quality_large: int = 85
    mine_only: bool = True


def parse_args() -> WarmupConfig:
    parser = argparse.ArgumentParser(description="Warm up Storage API media derivatives.")
    parser.add_argument("--api-base", required=True, help="Base URL of storage API (e.g. https://api-storage.arkturian.com)")
    parser.add_argument("--api-key", required=True, help="API key with access to the tenant")
    parser.add_argument("--tenant", required=True, help="Tenant ID (used for logging only)")
    parser.add_argument("--page-size", type=int, default=500, help="Number of objects per list request (default: 500)")
    parser.add_argument("--concurrency", type=int, default=8, help="Concurrent media requests (default: 8)")
    parser.add_argument("--retry-limit", type=int, default=3, help="Number of retries on network errors (default: 3)")
    parser.add_argument("--timeout", type=float, default=180.0, help="Total timeout per request (seconds)")
    parser.add_argument("--connect-timeout", type=float, default=30.0, help="Connection timeout (seconds)")
    parser.add_argument("--no-refresh", action="store_true", help="Skip refresh=true (will use cached versions)")
    parser.add_argument("--all-objects", action="store_true", help="Warm all objects (mine=false). Default: current tenant only.")

    args = parser.parse_args()

    widths = (130, 1300)
    cfg = WarmupConfig(
        api_base=args.api_base.rstrip("/"),
        api_key=args.api_key,
        tenant=args.tenant,
        page_size=args.page_size,
        concurrency=max(1, args.concurrency),
        request_timeout=args.timeout,
        connect_timeout=args.connect_timeout,
        retry_limit=max(1, args.retry_limit),
        widths=widths,
        mine_only=not args.all_objects,
    )
    cfg.refresh = not args.no_refresh  # type: ignore[attr-defined]
    return cfg


async def iter_object_ids(client: httpx.AsyncClient, cfg: WarmupConfig) -> AsyncIterator[int]:
    offset = 0
    total = None
    while True:
        params = {
            "mine": "true" if cfg.mine_only else "false",
            "limit": cfg.page_size,
            "offset": offset,
            "_": int(time.time()),
        }
        url = f"{cfg.api_base}/storage/list"
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        payload = resp.json()

        if total is None:
            total = payload.get("total", 0)
            print(f"📦 Tenant '{cfg.tenant}': {total} objects total")

        items = payload.get("items") or []
        if not items:
            break

        for item in items:
            object_id = item.get("id")
            if isinstance(object_id, int):
                yield object_id

        offset += len(items)
        if total is not None and offset >= total:
            break
        await asyncio.sleep(cfg.sleep_between_pages)


async def warm_single(
    client: httpx.AsyncClient,
    cfg: WarmupConfig,
    object_id: int,
    semaphore: asyncio.Semaphore,
) -> None:
    async with semaphore:
        for width in cfg.widths:
            params = {
                "width": width,
                "format": cfg.format,
                "quality": cfg.quality_large if width >= 1000 else cfg.quality_small,
            }
            if getattr(cfg, "refresh", True):
                params["refresh"] = "true"

            url = f"{cfg.api_base}/storage/media/{object_id}"

            for attempt in range(1, cfg.retry_limit + 1):
                try:
                    resp = await client.get(url, params=params)
                    resp.raise_for_status()
                    size = len(resp.content)
                    print(f"✅ Warmed object {object_id} width={width} (bytes={size})")
                    break
                except Exception as exc:
                    if attempt >= cfg.retry_limit:
                        print(f"❌ Failed object {object_id} width={width} after {attempt} attempts: {exc}")
                    else:
                        delay = cfg.sleep_between_retries * attempt
                        print(f"⚠️  Retry object {object_id} width={width} attempt {attempt}: {exc} (sleep {delay:.1f}s)")
                        await asyncio.sleep(delay)


async def warm_all(cfg: WarmupConfig) -> None:
    timeout = httpx.Timeout(
        timeout=cfg.request_timeout,
        connect=cfg.connect_timeout,
        read=cfg.request_timeout,
        write=cfg.request_timeout,
    )
    headers = {"X-API-KEY": cfg.api_key}

    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        semaphore = asyncio.Semaphore(cfg.concurrency)
        tasks: List[asyncio.Task[None]] = []
        count = 0
        start = time.perf_counter()
        async for object_id in iter_object_ids(client, cfg):
            count += 1
            task = asyncio.create_task(warm_single(client, cfg, object_id, semaphore))
            tasks.append(task)

        if not tasks:
            print("ℹ️  No objects found to warm up.")
            return

        print(f"🚀 Warming {count} objects with concurrency={cfg.concurrency}")
        await asyncio.gather(*tasks)
        duration = time.perf_counter() - start
        print(f"✅ Warmup complete for {count} objects in {duration:.1f}s")


def main() -> None:
    cfg = parse_args()
    try:
        asyncio.run(warm_all(cfg))
    except KeyboardInterrupt:
        print("Interrupted by user", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
