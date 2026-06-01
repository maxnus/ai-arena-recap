from fastapi import APIRouter, Depends, Request
from sqlmodel import Session

from ai_arena_recap.web.deps import get_session, render
from ai_arena_recap.web.rankings import all_rankings, most_viewed_bots

router = APIRouter()


@router.get("/rankings")
def rankings_page(request: Request, session: Session = Depends(get_session)):
    # all_rankings is cached (keyed on a data fingerprint). Page views change on
    # every request and aren't part of that fingerprint, so the "Most viewed bots"
    # card is built fresh here and spliced into the Community group. Rebuild the
    # group/list rather than mutating — the cached structure must stay untouched.
    most_viewed_card = {
        "title": "Most viewed bots",
        "value_label": "Views",
        "note": "Bot page views recorded so far (excludes crawlers)",
        "rows": most_viewed_bots(session),
    }
    groups = []
    for group in all_rankings(session):
        if group["title"] == "Community":
            group = {**group, "cards": [*group["cards"], most_viewed_card]}
        groups.append(group)
    return render(request, "rankings.html", groups=groups)
