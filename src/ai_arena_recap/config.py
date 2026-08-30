from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    aiarena_api_token: str
    aiarena_bot_id: int | None = None
    # The competition the site serves at its un-prefixed URLs. Every other
    # competition in the DB stays browsable as an archived season under
    # /s/<slug>/ — see web/season.py. Bump this at each season rollover (the
    # sync logs a warning and /healthz reports competition_closed when the
    # tracked competition ends).
    competition_id: int = 37
    # Closed seasons never change, so re-sync them at most this often. Cheap
    # insurance that a season archived mid-sync still completes itself.
    archive_refresh_seconds: int = 86400
    api_base_url: str = "https://aiarena.net/api"

    db_path: Path = Field(default=PROJECT_ROOT / "data" / "recap.sqlite")
    sync_interval_seconds: int = 300
    request_concurrency: int = 8
    # The aiarena.net API honours large page sizes (tested up to 10000), so pull
    # list endpoints in big pages to minimise the number of HTTP round-trips.
    api_page_size: int = 500
    # Starting page size for the backfill's per-bot participation sweep, which
    # the client shrinks when the server starts failing deep offsets. That
    # endpoint only offers offset pagination and offsets get costlier with
    # depth, so bigger pages pay that cost fewer times — but 5000, which
    # measured fastest against a rested API, produced 502/504s past offset
    # 25000 under sustained load. Start where it survives and let it adapt.
    # Separate from api_page_size so the live sync's small list calls stay small.
    backfill_page_size: int = 2000
    # The backfill's requests are far heavier than the live sync's: thousands of
    # rows from a deep offset, several in flight. The 30s default that suits
    # small list calls turns those into read timeouts, which retry, escalate,
    # and eventually exhaust — a first run lost 4 bots that way.
    backfill_timeout_seconds: float = 180.0
    # Bot metadata (name, race, type, wiki) changes rarely and the API has no
    # bulk-by-id fetch, so each refresh costs one request per bot. Refresh
    # infrequently; live ELO/stats come from competition-participations instead.
    bot_refresh_seconds: int = 21600

    replay_cache_enabled: bool = False
    replay_dir: Path = Field(default=PROJECT_ROOT / "data" / "replays")
    replay_max_age_days: int = 14
    replay_sync_interval_seconds: int = 300
    replay_download_concurrency: int = 4

    @property
    def database_url(self) -> str:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{self.db_path.as_posix()}"

    @property
    def replay_path(self) -> Path:
        self.replay_dir.mkdir(parents=True, exist_ok=True)
        return self.replay_dir


settings = Settings()
