from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class Competition(SQLModel, table=True):
    id: int = Field(primary_key=True)
    name: str
    status: str | None = None
    date_opened: datetime | None = None
    date_closed: datetime | None = None
    last_synced: datetime


class Bot(SQLModel, table=True):
    id: int = Field(primary_key=True)
    name: str
    user_id: int | None = None
    user_name: str | None = None
    plays_race: str | None = None
    type: str | None = None
    created: datetime | None = None
    game_display_id: str | None = None
    wiki_article_content: str | None = None
    last_synced: datetime


class CompetitionParticipation(SQLModel, table=True):
    __tablename__ = "competition_participation"
    __table_args__ = (UniqueConstraint("competition_id", "bot_id", name="uq_compbot"),)

    id: int = Field(primary_key=True)
    competition_id: int = Field(foreign_key="competition.id", index=True)
    bot_id: int = Field(foreign_key="bot.id", index=True)
    elo: int | None = None
    highest_elo: int | None = None
    division_num: int | None = None
    in_placements: bool = False
    active: bool = True
    match_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    tie_count: int = 0
    crash_count: int = 0
    win_perc: float | None = None
    loss_perc: float | None = None
    tie_perc: float | None = None
    crash_perc: float | None = None
    slug: str | None = None
    last_synced: datetime


class Round(SQLModel, table=True):
    __tablename__ = "round"
    __table_args__ = (UniqueConstraint("competition_id", "number", name="uq_compround"),)

    id: int = Field(primary_key=True)
    number: int = Field(index=True)
    competition_id: int = Field(foreign_key="competition.id", index=True)
    started: datetime | None = None
    finished: datetime | None = None
    complete: bool = False
    last_synced: datetime


class Map(SQLModel, table=True):
    __tablename__ = "map"

    id: int = Field(primary_key=True)
    name: str
    enabled: bool = True
    last_synced: datetime


class Match(SQLModel, table=True):
    id: int = Field(primary_key=True)
    round_id: int | None = Field(default=None, foreign_key="round.id", index=True)
    map_id: int | None = Field(default=None, foreign_key="map.id", index=True)
    created: datetime | None = None
    started: datetime | None = None
    result_type: str | None = None
    result_winner_bot_id: int | None = None
    result_created: datetime | None = None
    result_game_steps: int | None = None
    bot1_name: str | None = None
    bot2_name: str | None = None
    last_synced: datetime


class MatchParticipation(SQLModel, table=True):
    __tablename__ = "match_participation"

    id: int = Field(primary_key=True)
    match_id: int = Field(foreign_key="match.id", index=True)
    bot_id: int = Field(foreign_key="bot.id", index=True)
    participant_number: int
    starting_elo: int | None = None
    resultant_elo: int | None = None
    elo_change: int | None = None
    avg_step_time: float | None = None
    result: str | None = None
    result_cause: str | None = None
    last_synced: datetime
