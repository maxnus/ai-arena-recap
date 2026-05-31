from fastapi import APIRouter, Depends, Request
from sqlmodel import Session

from ai_arena_recap.web.deps import get_session, render
from ai_arena_recap.web.rankings import all_rankings, most_viewed_bots

router = APIRouter()


@router.get("/rankings")
def rankings_page(request: Request, session: Session = Depends(get_session)):
    # all_rankings is cached (keyed on a data fingerprint); page views change on
    # every request and aren't part of that fingerprint, so the Popularity card
    # is built fresh and appended to a *new* list — never mutate the cached one.
    groups = all_rankings(session)
    popularity = {
        "title": "Popularity",
        "cards": [{
            "title": "Most viewed bots",
            "value_label": "Views",
            "note": "Bot page views recorded so far (excludes crawlers)",
            "rows": most_viewed_bots(session),
        }],
    }
    return render(request, "rankings.html", groups=[*groups, popularity])
