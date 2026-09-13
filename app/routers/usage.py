"""Token-usage reporting for the current user."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import database, models
from ..auth import get_current_user

router = APIRouter()


@router.get("/api/usage", response_model=models.UsageSummary)
def usage_summary(user: dict = Depends(get_current_user)):
    uid = user["id"]
    totals = database.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(prompt_tokens),0) AS p, "
        "COALESCE(SUM(completion_tokens),0) AS c, COALESCE(SUM(total_tokens),0) AS t "
        "FROM usage WHERE user_id = ?",
        (uid,),
    )
    by_model = [
        {"model": r["model"], "requests": r["requests"], "total_tokens": r["total_tokens"]}
        for r in database.query(
            "SELECT model, COUNT(*) AS requests, COALESCE(SUM(total_tokens),0) AS total_tokens "
            "FROM usage WHERE user_id = ? GROUP BY model ORDER BY total_tokens DESC",
            (uid,),
        )
    ]
    by_day = [
        {"day": r["day"], "requests": r["requests"], "total_tokens": r["total_tokens"]}
        for r in database.query(
            "SELECT date(created_at,'unixepoch') AS day, COUNT(*) AS requests, "
            "COALESCE(SUM(total_tokens),0) AS total_tokens FROM usage WHERE user_id = ? "
            "GROUP BY day ORDER BY day DESC",
            (uid,),
        )
    ]
    return models.UsageSummary(
        total_requests=totals["n"] if totals else 0,
        total_tokens=totals["t"] if totals else 0,
        prompt_tokens=totals["p"] if totals else 0,
        completion_tokens=totals["c"] if totals else 0,
        by_model=by_model,
        by_day=by_day,
    )


@router.get("/api/usage/recent")
def usage_recent(user: dict = Depends(get_current_user), limit: int = 50):
    rows = database.query(
        "SELECT created_at, model, prompt_tokens, completion_tokens, total_tokens, status, "
        "tool_group_id FROM usage WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user["id"], limit),
    )
    return [dict(r) for r in rows]
