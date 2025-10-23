import uvicorn, secrets, time, threading, random, os
from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

lock = threading.Lock()

class PlayerState(BaseModel):
    user_id: str
    name: str
    card: List[Optional[int]]
    marks: List[bool]
    joined_at: float

class GameState(BaseModel):
    game_id: str
    host_id: str
    created_at: float
    draws: List[int]
    players: Dict[str, PlayerState]
    winner_ids: List[str]
    closed: bool

GAMES: Dict[str, GameState] = {}

def make_card() -> List[Optional[int]]:
    cols = []
    for c in range(5):
        pool = list(range(1 + c*15, 1 + c*15 + 15))
        random.shuffle(pool)
        cols.append(pool[:5])
    card = [cols[c][r] for r in range(5) for c in range(5)]
    card[12] = None
    return card

def new_marks() -> List[bool]:
    m = [False]*25
    m[12] = True
    return m

def check_line_bingo(marks: List[bool]) -> bool:
    lines = []
    for r in range(5): lines.append([r*5 + c for c in range(5)])
    for c in range(5): lines.append([r*5 + c for r in range(5)])
    lines += [[0,6,12,18,24],[4,8,12,16,20]]
    return any(all(marks[i] for i in line) for line in lines)

def marked_cells_are_valid(card: List[Optional[int]], marks: List[bool], draws: List[int]) -> bool:
    for i, marked in enumerate(marks):
        if not marked: continue
        if i == 12: continue
        v = card[i]
        if v is None: continue
        if v not in draws: return False
    return True

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
    closed: bool
    is_host: bool
    card: Optional[List[Optional[int]]] = None
    marks: Optional[List[bool]] = None
    has_bingo: bool = False

class MarkReq(BaseModel):
    user_id: str
    index: int
    marked: bool

class ClaimReq(BaseModel):
    user_id: str
class ClaimRes(BaseModel):
    valid: bool
    winner_ids: List[str]

@app.post("/games", response_model=CreateGameRes)
def create_game(req: CreateGameReq):
    with lock:
        gid = secrets.token_urlsafe(6)
        GAMES[gid] = GameState(
            game_id=gid, host_id=req.host_id, created_at=time.time(),
            draws=[], players={}, winner_ids=[], closed=False
        )
        GAMES[gid].players[req.host_id] = PlayerState(
            user_id=req.host_id, name=req.host_name[:64],
            card=make_card(), marks=new_marks(), joined_at=time.time()
        )
        return CreateGameRes(game_id=gid)

@app.post("/games/{gid}/join")
def join_game(gid: str, req: JoinReq):
    with lock:
        g = GAMES.get(gid)
        if not g or g.closed: raise HTTPException(404, "Game not found or closed")
        if len(g.players) >= 400 and req.user_id not in g.players:
            raise HTTPException(403, "Game is full (400)")
        if req.user_id not in g.players:
            g.players[req.user_id] = PlayerState(
                user_id=req.user_id, name=req.name[:64],
                card=make_card(), marks=new_marks(), joined_at=time.time()
            )
        return {"ok": True}

@app.post("/games/{gid}/draw")
def draw_number(gid: str, user_id: str):
    with lock:
        g = GAMES.get(gid)
        if not g or g.closed: raise HTTPException(404, "Game not found or closed")
        if user_id != g.host_id: raise HTTPException(403, "Only host can draw")
        if len(g.draws) >= 75: raise HTTPException(400, "All numbers drawn")
        remaining = [n for n in range(1,76) if n not in g.draws]
        nxt = random.choice(remaining)
        g.draws.append(nxt)
        return {"next_number": nxt, "draws": g.draws}

@app.post("/games/{gid}/mark")
def mark_cell(gid: str, req: MarkReq):
    with lock:
        g = GAMES.get(gid)
        if not g: raise HTTPException(404, "Game not found")
        p = g.players.get(req.user_id)
        if not p: raise HTTPException(404, "Player not in game")
        if req.index < 0 or req.index > 24: raise HTTPException(400, "Index out of range")
        if req.index == 12: p.marks[12] = True
        else: p.marks[req.index] = bool(req.marked)
        return {"ok": True}

@app.post("/games/{gid}/state", response_model=StateRes)
def get_state(gid: str, req: StateReq):
    with lock:
        g = GAMES.get(gid)
        if not g: raise HTTPException(404, "Game not found")
        is_host = (req.user_id == g.host_id)
        res = StateRes(
            game_id=g.game_id, host_id=g.host_id, draws=g.draws,
            players_count=len(g.players), winner_ids=g.winner_ids,
            closed=g.closed, is_host=is_host
        )
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
        if not g or g.closed: raise HTTPException(404, "Game not found or closed")
        p = g.players.get(req.user_id)
        if not p: raise HTTPException(404, "Player not in game")
        valid = marked_cells_are_valid(p.card, p.marks, g.draws) and check_line_bingo(p.marks)
        if valid and req.user_id not in g.winner_ids:
            g.winner_ids.append(req.user_id)
        return ClaimRes(valid=valid, winner_ids=g.winner_ids)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
