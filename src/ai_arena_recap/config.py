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
    competition_id: int = 36
    api_base_url: str = "https://aiarena.net/api"

    db_path: Path = Field(default=PROJECT_ROOT / "data" / "recap.sqlite")
    sync_interval_seconds: int = 300
    request_concurrency: int = 8
    bot_refresh_seconds: int = 24 * 3600

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
