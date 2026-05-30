from fastapi import APIRouter, Depends, Request
from sqlmodel import Session

from ai_arena_recap.web.deps import get_session, render
from ai_arena_recap.web.rankings import all_rankings

router = APIRouter()


@router.get("/rankings")
def rankings_page(request: Request, session: Session = Depends(get_session)):
    groups = all_rankings(session)
    return render(request, "rankings.html", groups=groups)
