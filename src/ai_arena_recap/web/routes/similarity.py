from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlmodel import Session

from ai_arena_recap.web.deps import get_session, render
from ai_arena_recap.web.similarity import MIN_COMMON, matrix, races, top_pairs

router = APIRouter()


@router.get("/similarity")
def similarity_page(request: Request, session: Session = Depends(get_session)):
    available = races(session)
    # min_common is passed through rather than written into the copy, so the
    # number the page quotes cannot drift from the one the model enforces.
    return render(request, "similarity.html", races=available,
                  default_race=available[0]["code"] if available else None,
                  min_common=MIN_COMMON)


@router.get("/api/similarity/pairs.json")
def similarity_pairs_json(session: Session = Depends(get_session)) -> dict:
    return {"data": top_pairs(session)}


@router.get("/api/similarity/matrix.json")
def similarity_matrix_json(
    race: str = Query(..., min_length=1, max_length=1),
    session: Session = Depends(get_session),
) -> dict:
    race = race.upper()
    if race not in {r["code"] for r in races(session)}:
        raise HTTPException(status_code=404, detail="No similarity matrix for that race")
    return matrix(session, race)
