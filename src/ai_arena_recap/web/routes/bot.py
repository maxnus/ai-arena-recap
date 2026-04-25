from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, select

from ai_arena_recap.config import settings
from ai_arena_recap.models import Bot, CompetitionParticipation
from ai_arena_recap.web.deps import get_session, render

router = APIRouter()


@router.get("/bots/{bot_id}")
def bot_page(bot_id: int, request: Request, session: Session = Depends(get_session)):
    bot = session.get(Bot, bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    cp = session.exec(
        select(CompetitionParticipation)
        .where(
            CompetitionParticipation.bot_id == bot_id,
            CompetitionParticipation.competition_id == settings.competition_id,
        )
    ).first()
    return render(request, "bot.html", bot=bot, cp=cp)
