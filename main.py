import os
import sqlite3
import secrets
import hashlib
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "app.db"))

def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def add_column(conn, table: str, column: str, definition: str):
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

def init_db():
    with get_db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('guardian', 'senior')),
            invite_code TEXT NOT NULL UNIQUE, linked_user_id INTEGER, created_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT, activity_type TEXT NOT NULL, question TEXT NOT NULL,
            answer TEXT, correct_answer TEXT, is_correct INTEGER, encouragement TEXT, completed_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS quests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
            reflection TEXT DEFAULT '', completed_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS app_state (
            state_key TEXT PRIMARY KEY, state_value TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS quest_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, quest_key TEXT NOT NULL,
            title TEXT NOT NULL, role TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
            reflection TEXT DEFAULT '', result_summary TEXT DEFAULT '', completed_at TEXT,
            is_shared INTEGER NOT NULL DEFAULT 0, media_kind TEXT DEFAULT '', media_count INTEGER NOT NULL DEFAULT 0,
            UNIQUE(user_id, quest_key))""")
        for table, column, definition in [
            ("activities", "user_id", "INTEGER"), ("quests", "user_id", "INTEGER"), ("app_state", "user_id", "INTEGER"),
            ("quest_progress", "is_shared", "INTEGER NOT NULL DEFAULT 0"), ("quest_progress", "media_kind", "TEXT DEFAULT ''"),
            ("quest_progress", "media_count", "INTEGER NOT NULL DEFAULT 0"),
        ]: add_column(conn, table, column, definition)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
init_db()

def now(): return datetime.now().isoformat(timespec="seconds")
def password_hash(password: str):
    salt = secrets.token_hex(16)
    return f"{salt}${hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 120000).hex()}"
def password_ok(password: str, stored: str):
    salt, digest = stored.split("$", 1)
    return secrets.compare_digest(hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120000).hex(), digest)
def make_code(conn):
    while True:
        code = "CARE-" + secrets.token_hex(3).upper()
        if not conn.execute("SELECT 1 FROM users WHERE invite_code = ?", (code,)).fetchone(): return code
def current_user(authorization: Optional[str] = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "): raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    with get_db() as conn:
        row = conn.execute("SELECT u.* FROM users u JOIN sessions s ON u.id = s.user_id WHERE s.token = ?", (authorization.removeprefix("Bearer "),)).fetchone()
    if not row: raise HTTPException(status_code=401, detail="로그인이 만료되었습니다.")
    return row
def profile(row):
    return {"id": row["id"], "name": row["name"], "email": row["email"], "role": row["role"], "invite_code": row["invite_code"], "linked_user_id": row["linked_user_id"], "is_linked": bool(row["linked_user_id"])}
def activity_owner(user): return user["linked_user_id"] if user["role"] == "guardian" and user["linked_user_id"] else user["id"]
def progress_dict(row): return dict(row) | {"completed": bool(row["completed"]), "is_shared": bool(row["is_shared"])}

class SignUpInput(BaseModel):
    name: str = Field(min_length=1, max_length=30); email: str = Field(min_length=3, max_length=100); password: str = Field(min_length=4, max_length=100); role: str; partner_code: str = ""
class LoginInput(BaseModel): email: str; password: str
class LinkInput(BaseModel): partner_code: str
class ActivityInput(BaseModel): activity_type: str; question: str; answer: str; correct_answer: str; is_correct: bool; encouragement: str
class QuestInput(BaseModel): title: str; completed: bool; reflection: str = ""
class QuestProgressInput(BaseModel):
    quest_key: str; title: str; reflection: str = ""; result_summary: str = ""; is_shared: bool = False; media_kind: str = ""; media_count: int = 0
class AppStateInput(BaseModel): mode: str = "home"; step: int = 0

@app.get("/api/health")
def health(): return {"ok": True}
@app.post("/api/auth/signup")
def signup(payload: SignUpInput):
    if payload.role not in ("guardian", "senior"): raise HTTPException(status_code=400, detail="가입 유형을 선택해 주세요.")
    with get_db() as conn:
        linked_id = None; code = payload.partner_code.strip().upper()
        if code:
            partner = conn.execute("SELECT * FROM users WHERE invite_code = ?", (code,)).fetchone()
            if not partner: raise HTTPException(status_code=400, detail="연결 코드를 찾을 수 없습니다.")
            if partner["role"] == payload.role: raise HTTPException(status_code=400, detail="보호자와 시니어 코드는 서로 연결할 수 있어요.")
            if partner["linked_user_id"]: raise HTTPException(status_code=400, detail="이미 연결된 코드입니다.")
            linked_id = partner["id"]
        try:
            cursor = conn.execute("INSERT INTO users (name,email,password_hash,role,invite_code,linked_user_id,created_at) VALUES (?,?,?,?,?,?,?)", (payload.name.strip(), payload.email.strip().lower(), password_hash(payload.password), payload.role, make_code(conn), linked_id, now()))
        except sqlite3.IntegrityError: raise HTTPException(status_code=400, detail="이미 사용 중인 이메일입니다.")
        user_id = cursor.lastrowid
        if linked_id: conn.execute("UPDATE users SET linked_user_id = ? WHERE id = ?", (user_id, linked_id))
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone(); token = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO sessions (token,user_id,created_at) VALUES (?,?,?)", (token, user_id, now()))
    return {"token": token, "user": profile(user)}
@app.post("/api/auth/login")
def login(payload: LoginInput):
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = ?", (payload.email.strip().lower(),)).fetchone()
        if not user or not password_ok(payload.password, user["password_hash"]): raise HTTPException(status_code=401, detail="이메일 또는 비밀번호를 확인해 주세요.")
        token = secrets.token_urlsafe(32); conn.execute("INSERT INTO sessions (token,user_id,created_at) VALUES (?,?,?)", (token, user["id"], now()))
    return {"token": token, "user": profile(user)}
@app.post("/api/auth/link")
def link_partner(payload: LinkInput, user=Depends(current_user)):
    if user["linked_user_id"]: raise HTTPException(status_code=400, detail="이미 연결된 계정입니다.")
    with get_db() as conn:
        partner = conn.execute("SELECT * FROM users WHERE invite_code = ?", (payload.partner_code.strip().upper(),)).fetchone()
        if not partner: raise HTTPException(status_code=400, detail="연결 코드를 찾을 수 없습니다.")
        if partner["role"] == user["role"]: raise HTTPException(status_code=400, detail="반대 역할의 코드를 입력해 주세요.")
        if partner["linked_user_id"]: raise HTTPException(status_code=400, detail="이미 연결된 코드입니다.")
        conn.execute("UPDATE users SET linked_user_id = ? WHERE id = ?", (partner["id"], user["id"])); conn.execute("UPDATE users SET linked_user_id = ? WHERE id = ?", (user["id"], partner["id"]))
        updated = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    return {"user": profile(updated)}
@app.post("/api/auth/logout")
def logout(authorization: Optional[str] = Header(default=None)):
    if authorization and authorization.startswith("Bearer "):
        with get_db() as conn: conn.execute("DELETE FROM sessions WHERE token = ?", (authorization.removeprefix("Bearer "),))
    return {"ok": True}
@app.get("/api/activities")
def list_activities(days: Optional[int] = None, activity_type: Optional[str] = None, user=Depends(current_user)):
    sql, params = "SELECT * FROM activities WHERE user_id = ?", [activity_owner(user)]
    if days: sql += " AND completed_at >= datetime('now', ?)"; params.append(f"-{days} days")
    if activity_type and activity_type != "전체": sql += " AND activity_type = ?"; params.append(activity_type)
    with get_db() as conn: rows = conn.execute(sql + " ORDER BY completed_at DESC", params).fetchall()
    return [dict(row) | {"is_correct": bool(row["is_correct"])} for row in rows]
@app.post("/api/activities")
def create_activity(payload: ActivityInput, user=Depends(current_user)):
    if user["role"] != "senior": raise HTTPException(status_code=403, detail="시니어 계정에서만 활동을 기록할 수 있습니다.")
    with get_db() as conn:
        cursor = conn.execute("INSERT INTO activities (activity_type,question,answer,correct_answer,is_correct,encouragement,completed_at,user_id) VALUES (?,?,?,?,?,?,?,?)", (payload.activity_type,payload.question,payload.answer,payload.correct_answer,int(payload.is_correct),payload.encouragement,now(),user["id"]))
        row = conn.execute("SELECT * FROM activities WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row) | {"is_correct": bool(row["is_correct"])}
@app.get("/api/quests")
def list_quests(user=Depends(current_user)):
    with get_db() as conn: rows = conn.execute("SELECT * FROM quests WHERE user_id = ? ORDER BY COALESCE(completed_at, '') DESC, id DESC", (activity_owner(user),)).fetchall()
    return [dict(row) | {"completed": bool(row["completed"])} for row in rows]
@app.post("/api/quests")
def create_quest(payload: QuestInput, user=Depends(current_user)):
    if user["role"] != "senior": raise HTTPException(status_code=403, detail="시니어 계정에서만 퀘스트를 기록할 수 있습니다.")
    with get_db() as conn:
        cursor = conn.execute("INSERT INTO quests (title,completed,reflection,completed_at,user_id) VALUES (?,?,?,?,?)", (payload.title,int(payload.completed),payload.reflection.strip(),now() if payload.completed else None,user["id"]))
        row = conn.execute("SELECT * FROM quests WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row) | {"completed": bool(row["completed"])}
@app.get("/api/quest-progress")
def quest_progress(user=Depends(current_user)):
    with get_db() as conn: rows = conn.execute("SELECT * FROM quest_progress WHERE user_id = ? ORDER BY completed_at DESC", (user["id"],)).fetchall()
    return [progress_dict(row) for row in rows]
@app.get("/api/family-timeline")
def family_timeline(user=Depends(current_user)):
    ids = [user["id"]]
    if user["linked_user_id"]: ids.append(user["linked_user_id"])
    marks = ",".join("?" for _ in ids)
    with get_db() as conn:
        rows = conn.execute(f"""SELECT q.*, u.name AS author_name FROM quest_progress q JOIN users u ON u.id=q.user_id
            WHERE q.user_id = ? OR (q.user_id IN ({marks}) AND q.is_shared = 1)
            ORDER BY q.completed_at DESC""", [user["id"], *ids]).fetchall()
    return [progress_dict(row) | {"is_mine": row["user_id"] == user["id"]} for row in rows]
@app.post("/api/quest-progress")
def complete_quest(payload: QuestProgressInput, user=Depends(current_user)):
    with get_db() as conn:
        conn.execute("""INSERT INTO quest_progress (user_id,quest_key,title,role,completed,reflection,result_summary,completed_at,is_shared,media_kind,media_count)
          VALUES (?,?,?,?,1,?,?,?,?,?,?) ON CONFLICT(user_id,quest_key) DO UPDATE SET completed=1, reflection=excluded.reflection, result_summary=excluded.result_summary, completed_at=excluded.completed_at, is_shared=excluded.is_shared, media_kind=excluded.media_kind, media_count=excluded.media_count""", (user["id"],payload.quest_key,payload.title,user["role"],payload.reflection.strip(),payload.result_summary.strip(),now(),int(payload.is_shared),payload.media_kind,max(0,payload.media_count)))
        row = conn.execute("SELECT * FROM quest_progress WHERE user_id = ? AND quest_key = ?", (user["id"], payload.quest_key)).fetchone()
    return progress_dict(row)
@app.get("/api/summary")
def summary(user=Depends(current_user)):
    owner = activity_owner(user)
    with get_db() as conn:
        activities = conn.execute("SELECT * FROM activities WHERE user_id = ? ORDER BY completed_at DESC", (owner,)).fetchall(); quests = conn.execute("SELECT * FROM quests WHERE user_id = ?", (owner,)).fetchall(); progress = conn.execute("SELECT * FROM quest_progress WHERE user_id = ?", (user["id"],)).fetchall()
    total = len(activities); correct = sum(row["is_correct"] for row in activities); types = {}
    for row in activities: types[row["activity_type"]] = types.get(row["activity_type"], 0) + 1
    return {"recent_activity": activities[0]["completed_at"] if activities else None, "participation_count": total, "accuracy": round(correct / total * 100) if total else 0, "completed_quests": sum(row["completed"] for row in quests) + sum(row["completed"] for row in progress), "by_type": [{"name": key, "value": value} for key, value in types.items()]}
@app.get("/api/activities/{activity_id}")
def detail_activity(activity_id: int, user=Depends(current_user)):
    with get_db() as conn: row = conn.execute("SELECT * FROM activities WHERE id = ? AND user_id = ?", (activity_id, activity_owner(user))).fetchone()
    if not row: raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")
    return dict(row) | {"is_correct": bool(row["is_correct"])}
@app.get("/api/app-state")
def get_app_state(user=Depends(current_user)):
    with get_db() as conn: rows = conn.execute("SELECT state_key,state_value FROM app_state WHERE user_id = ?", (user["id"],)).fetchall()
    values = {row["state_key"]: row["state_value"] for row in rows}; return {"mode": values.get("mode", "home"), "step": int(values.get("step", "0"))}
@app.put("/api/app-state")
def save_app_state(payload: AppStateInput, user=Depends(current_user)):
    with get_db() as conn:
        for key, value in (("mode",payload.mode),("step",str(max(0,payload.step)))):
            conn.execute("DELETE FROM app_state WHERE user_id = ? AND state_key = ?", (user["id"],key)); conn.execute("INSERT INTO app_state (state_key,state_value,updated_at,user_id) VALUES (?,?,?,?)", (key,value,now(),user["id"]))
    return {"mode": payload.mode, "step": max(0,payload.step)}
