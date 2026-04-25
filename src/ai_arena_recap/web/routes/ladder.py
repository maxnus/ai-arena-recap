from fastapi import APIRouter, Depends, Request
from sqlmodel import Session, select

from ai_arena_recap.config import settings
from ai_arena_recap.models import Bot, CompetitionParticipation
from ai_arena_recap.web.deps import get_session, render

router = APIRouter()


@router.get("/")
def ladder(request: Request, session: Session = Depends(get_session)):
    rows = session.exec(
        select(CompetitionParticipation, Bot)
        .join(Bot, CompetitionParticipation.bot_id == Bot.id)
        .where(CompetitionParticipation.competition_id == settings.competition_id)
        .where(CompetitionParticipation.active == True)  # noqa: E712
        .order_by(
            CompetitionParticipation.division_num.asc().nullslast(),
            CompetitionParticipation.elo.desc().nullslast(),
        )
    ).all()
    standings = [{"cp": cp, "bot": bot, "rank": i + 1} for i, (cp, bot) in enumerate(rows)]
    return render(request, "ladder.html", standings=standings)
