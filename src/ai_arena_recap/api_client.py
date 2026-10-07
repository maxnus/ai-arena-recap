"""The aiarena.net API client, configured from the recap's settings.

The client itself is the aiarena-api package (https://github.com/maxnus/aiarena-api):
retries, pacing and the bulk participation pager live there.
"""
from aiarena_api import AiArenaClient

from ai_arena_recap.config import settings


def new_client(*, timeout: float = 30.0) -> AiArenaClient:
    """A client with the recap's token, server, concurrency and page sizes."""
    return AiArenaClient(
        token=settings.aiarena_api_token,
        base_url=settings.api_base_url,
        concurrency=settings.request_concurrency,
        timeout=timeout,
        page_size=settings.api_page_size,
        bulk_page_size=settings.backfill_page_size,
    )
