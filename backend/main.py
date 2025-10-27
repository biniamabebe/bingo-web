from __future__ import annotations

import os
import random
import secrets
import threading
import time
from typing import Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# -----------------------------------------------------------------------------
# App & CORS
# -----------------------------------------------------------------------------
app = FastAPI(title="Bingo Backend", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # lock down to your Vercel origin if you want
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------------------------------------------------------
# In-memory state (simple for demo; swap to Redis/DB for persistence)
# -----------------------------------------------------------------------------
lock = threading.Lock()


class PlayerState(BaseModel):
    user_id: str
    name: str
    card: List[Optional[int]]          # 25 entries; center idx 12 = None (FREE)
    marks: List[bool]                  # 25 booleans; idx 12 True
    joined_at: float


class GameState(BaseModel):
    game_id: str
    host_id: str
    created_at: float
    draws: List[int]                   # numbers called (1..75)
    players: Dict[str, PlayerState]
    winner_ids: List[str]
    closed: bool


GAMES: Dict[str, GameState] = {}

# Auto-draw controller: gid -> {"stop": Event, "thread": Thread, "interval": int}
AUTO: Dict[str, Dict[str, object]] = {}


# -----------------------------------------------------------------------------
# Helper functions
# -----------------------------------------------------------------------------
def make_card() -> List[Optional[int]]:
    """Generate standard 5x5 Bingo card, B/I/N/G/O columns, center FREE."""
    cols = []
    for c in range(5):
        pool = list(range(1 + c * 15, 1 + c * 15 + 15))
        random.shuffle(pool)
        cols.append(pool[:5])
    # flatten by rows
    card = [cols[c][r] for r in range(5) for c in range(5)]
    card[12] = None  # FREE
    return card


def new_marks() -> List[bool]:
    m = [False] * 25
    m[12] = True  # FREE always marked
    return m


def check_line_bingo(marks: List[bool]) -> bool:
    lines = []
    # rows
    for r in range(5):
        lines.append([r * 5 + c for c in range(5)])
    # cols
    for c in range(5):
        lines.append([r * 5 + c for r in range(5)])
    # diagonals
    lines += [[0, 6, 12, 18, 24], [4, 8, 12, 16, 20]]
    return any(all(marks[i] for i in line) for line in lines)


def marked_cells_are_valid(card: List[Optional[int]], marks: List[bool], draws: List[int]) -> bool:
    """Every marked cell (except FREE) must be a number that has been drawn."""
    for i, m in enumerate(marks):
        if not m:
            continue
        if i == 12:  # FREE
            continue
        v = card[i]
        if v is None:
            continue
        if v not in draws:
            return False
    return True


def remaining_numbers(draws: List[int]) -> List[int]:
    return [n for n in range(1, 76) if n not in draws]


# -----------------------------------------------------------------------------
# Auto-draw worker
# -----------------------------------------------------------------------------
def _auto_draw_loop(gid: str):
    """Background loop that draws a number every interval seconds."""
    while True:
        with lock:
            info = AUTO.get(gid)
            g = GAMES.get(gid)
            if not info or not g or g.closed:
                # game gone or stopped
                AUTO.pop(gid, None)
                return
            stop: threading.Event = info["stop"]  # type: ignore
            interval: int = int(info.get("interval", 5))  # type: ignore

        if stop.wait(timeout=max(2, interval)):
            # stop signaled
            with lock:
                AUTO.pop(gid, None)
            return

        with lock:
            g = GAMES.get(gid)
            if not g:
                AUTO.pop(gid, None)
                return
            if len(g.draws) >= 75:
                AUTO.pop(gid, None)
                return
            rem = remaining_numbers(g.draws)
            if not rem:
                AUTO.pop(gid, None)
                return
            nxt = random.choice(rem)
            g.draws.append(nxt)


def start_auto_draw(gid: str, interval: int = 5):
    with lock:
        if gid in AUTO:
            # update interval
            AUTO[gid]["interval"] = interval
            return
        stop = threading.Event()
        t = threading.Thread(target=_auto_draw_loop, args=(gid,), daemon=True)
        AUTO[gid] = {"stop": stop, "thread": t, "interval": interval}
        t.start()


def stop_auto_draw(gid: str):
    with lock:
        info = AUTO.get(gid)
        if info:
            stop: threading.Event = info["stop"]  # type: ignore
            stop.set()


def stop_all_auto():
    with lock:
        for info in list(AUTO.values()):
            stop: threading.Event = info["stop"]  # type: ignore
            stop.set()
        AUTO.clear()


# -----------------------------------------------------------------------------
# Request/Response models
# -----------------------------------------------------------------------------
class CreateGameReq(BaseModel):
    host_id: str
    host_name: str


class CreateGameRes(BaseModel):
    game_id: str


class JoinReq(BaseModel):
    user_id: str
    name: str


class StateReq(BaseModel):
    user_id: str


class StateRes(BaseModel):
    game_id: str
    host_id: str
    draws: List[int]
    players_count: int
    winner_ids: List[str]
    winner_names: List[str] = []     # NEW: names for all winners
    closed: bool
    is_host: bool
    card: Optional[List[Optional[int]]] = None
    marks: Optional[List[bool]] = None
    has_bingo: bool = False


class MarkReq(BaseModel):
    user_id: str
    index: int = Field(ge=0, le=24)
    marked: bool


class ClaimReq(BaseModel):
    user_id: str


class ClaimRes(BaseModel):
    valid: bool
    winner_ids: List[str]
    winner_names: List[str] = []     # NEW: names echoed after claim


class AutoReq(BaseModel):
    user_id: str
    on: bool
    interval: int = Field(5, ge=2, le=60)


# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------
@app.post("/games", response_model=CreateGameRes)
def create_game(req: CreateGameReq):
    """Create a game; host auto-joins; auto-draw starts (every 5s)."""
    with lock:
        gid = secrets.token_urlsafe(6)
        GAMES[gid] = GameState(
            game_id=gid,
            host_id=req.host_id,
            created_at=time.time(),
            draws=[],
            players={},
            winner_ids=[],
            closed=False,
        )
        # host joins
        GAMES[gid].players[req.host_id] = PlayerState(
            user_id=req.host_id,
            name=req.host_name[:64],
            card=make_card(),
            marks=new_marks(),
            joined_at=time.time(),
        )
    # start auto-draw outside lock
    start_auto_draw(gid, interval=5)
    return CreateGameRes(game_id=gid)


@app.post("/games/{gid}/join")
def join_game(gid: str, req: JoinReq):
    with lock:
        g = GAMES.get(gid)
        if not g or g.closed:
            raise HTTPException(status_code=404, detail="Game not found or closed")
        if len(g.players) >= 400 and req.user_id not in g.players:
            raise HTTPException(status_code=403, detail="Game is full (400)")
        if req.user_id not in g.players:
            g.players[req.user_id] = PlayerState(
                user_id=req.user_id,
                name=req.name[:64],
                card=make_card(),
                marks=new_marks(),
                joined_at=time.time(),
            )
    return {"ok": True}


@app.post("/games/{gid}/draw")
def draw_number(gid: str, user_id: str):
    """Manual draw (still available); only host can call."""
    with lock:
        g = GAMES.get(gid)
        if not g or g.closed:
            raise HTTPException(status_code=404, detail="Game not found or closed")
        if user_id != g.host_id:
            raise HTTPException(status_code=403, detail="Only host can draw")
        if len(g.draws) >= 75:
            raise HTTPException(status_code=400, detail="All numbers drawn")
        rem = remaining_numbers(g.draws)
        if not rem:
            raise HTTPException(status_code=400, detail="No numbers remaining")
        nxt = random.choice(rem)
        g.draws.append(nxt)
        return {"next_number": nxt, "draws": g.draws}


@app.post("/games/{gid}/mark")
def mark_cell(gid: str, req: MarkReq):
    with lock:
        g = GAMES.get(gid)
        if not g:
            raise HTTPException(404, "Game not found")
        p = g.players.get(req.user_id)
        if not p:
            raise HTTPException(404, "Player not in game")
        if req.index == 12:
            p.marks[12] = True  # FREE always marked
        else:
            p.marks[req.index] = bool(req.marked)
    return {"ok": True}


@app.post("/games/{gid}/state", response_model=StateRes)
def get_state(gid: str, req: StateReq):
    with lock:
        g = GAMES.get(gid)
        if not g:
            raise HTTPException(404, "Game not found")
        is_host = req.user_id == g.host_id
        res = StateRes(
            game_id=g.game_id,
            host_id=g.host_id,
            draws=list(g.draws),
            players_count=len(g.players),
            winner_ids=list(g.winner_ids),
            closed=g.closed,
            is_host=is_host,
        )
        # fill winner names
        res.winner_names = [g.players[uid].name for uid in g.winner_ids if uid in g.players]
        # include the caller's card/marks
        p = g.players.get(req.user_id)
        if p:
            res.card = p.card
            res.marks = p.marks
            res.has_bingo = marked_cells_are_valid(p.card, p.marks, g.draws) and check_line_bingo(p.marks)
        return res


@app.post("/games/{gid}/claim", response_model=ClaimRes)
def claim_bingo(gid: str, req: ClaimReq):
    with lock:
        g = GAMES.get(gid)
        if not g or g.closed:
            raise HTTPException(404, "Game not found or closed")
        p = g.players.get(req.user_id)
        if not p:
            raise HTTPException(404, "Player not in game")
        valid = marked_cells_are_valid(p.card, p.marks, g.draws) and check_line_bingo(p.marks)
        if valid and req.user_id not in g.winner_ids:
            g.winner_ids.append(req.user_id)
        names = [g.players[uid].name for uid in g.winner_ids if uid in g.players]
        return ClaimRes(valid=valid, winner_ids=list(g.winner_ids), winner_names=names)


@app.post("/games/{gid}/auto")
def set_auto_draw(gid: str, req: AutoReq):
    """Toggle/adjust auto-draw. Only host may call."""
    with lock:
        g = GAMES.get(gid)
        if not g:
            raise HTTPException(404, "Game not found")
        if req.user_id != g.host_id:
            raise HTTPException(403, "Only host can change auto-draw")
    if req.on:
        start_auto_draw(gid, interval=req.interval)
    else:
        stop_auto_draw(gid)
    return {"ok": True, "on": req.on, "interval": req.interval}


# -----------------------------------------------------------------------------
# Graceful shutdown (stop all auto threads)
# -----------------------------------------------------------------------------
@app.on_event("shutdown")
def _shutdown():
    stop_all_auto()


# -----------------------------------------------------------------------------
# Local run (not used on Railway when Start Command is set)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
