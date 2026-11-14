import os
import sqlite3
import secrets
import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Optional
from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "app.db"))
# 타임라인 사진 저장 폴더. 지정하지 않으면 DB 파일 옆의 uploads 폴더를 씁니다.
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "uploads"))
MEDIA_MAX_BYTES = 5 * 1024 * 1024
MEDIA_MAX_PER_POST = 5
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
        # app_state는 state_key만 기본키라 사용자가 둘 이상이면 저장이 실패해서, 사용자별 표를 따로 둡니다.
        conn.execute("""CREATE TABLE IF NOT EXISTS user_app_state (
            user_id INTEGER NOT NULL, state_key TEXT NOT NULL, state_value TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(user_id, state_key))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS quest_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, quest_key TEXT NOT NULL,
            title TEXT NOT NULL, role TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
            reflection TEXT DEFAULT '', result_summary TEXT DEFAULT '', completed_at TEXT,
            is_shared INTEGER NOT NULL DEFAULT 0, media_kind TEXT DEFAULT '', media_count INTEGER NOT NULL DEFAULT 0,
            UNIQUE(user_id, quest_key))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS mood_checkins (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            mood TEXT NOT NULL CHECK(mood IN ('good', 'okay', 'hard')),
            checkin_date TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(user_id, checkin_date))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS meetup_interests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, meetup_key TEXT NOT NULL,
            created_at TEXT NOT NULL, UNIQUE(user_id, meetup_key))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS post_media (
            id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            file_name TEXT NOT NULL, content_type TEXT NOT NULL, size_bytes INTEGER NOT NULL, created_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS timeline_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, author_id INTEGER NOT NULL, quest_key TEXT NOT NULL,
            title TEXT NOT NULL, body TEXT DEFAULT '', result_summary TEXT DEFAULT '', created_at TEXT NOT NULL,
            deleted_at TEXT, source_progress_id INTEGER UNIQUE)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS post_likes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL, UNIQUE(post_id, user_id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS post_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            body TEXT NOT NULL, created_at TEXT NOT NULL, deleted_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL, category TEXT NOT NULL, source_name TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL DEFAULT '',
            author_id INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('draft', 'published')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, published_at TEXT)""")
        for table, column, definition in [
            ("activities", "user_id", "INTEGER"), ("quests", "user_id", "INTEGER"), ("app_state", "user_id", "INTEGER"),
            ("quest_progress", "is_shared", "INTEGER NOT NULL DEFAULT 0"), ("quest_progress", "media_kind", "TEXT DEFAULT ''"),
            ("quest_progress", "media_count", "INTEGER NOT NULL DEFAULT 0"),
            ("timeline_posts", "source_progress_id", "INTEGER UNIQUE"),
        ]: add_column(conn, table, column, definition)
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
init_db()
try:
    KOREA = ZoneInfo("Asia/Seoul")
except ZoneInfoNotFoundError:
    KOREA = timezone(timedelta(hours=9), name="Asia/Seoul")
def now(): return datetime.now(KOREA).isoformat(timespec="seconds")
def today_korea(): return datetime.now(KOREA).date().isoformat()
def migrate_shared_progress():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM quest_progress WHERE is_shared = 1 AND completed = 1").fetchall()
        for row in rows:
            conn.execute("""INSERT OR IGNORE INTO timeline_posts
                (author_id,quest_key,title,body,result_summary,created_at,source_progress_id)
                VALUES (?,?,?,?,?,?,?)""", (row["user_id"], row["quest_key"], row["title"], row["reflection"] or "", row["result_summary"] or "", row["completed_at"] or now(), row["id"]))
migrate_shared_progress()
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
def admin_emails(): return {email.strip().lower() for email in os.getenv("ADMIN_EMAILS", "").split(",") if email.strip()}
def is_admin(user): return user["email"].strip().lower() in admin_emails()
def require_admin(user=Depends(current_user)):
    if not is_admin(user): raise HTTPException(status_code=403, detail="관리자만 글을 관리할 수 있습니다.")
    return user
def profile(row):
    return {"id": row["id"], "name": row["name"], "email": row["email"], "role": row["role"], "invite_code": row["invite_code"], "linked_user_id": row["linked_user_id"], "is_linked": bool(row["linked_user_id"]), "is_admin": is_admin(row)}
def activity_owner(user): return user["linked_user_id"] if user["role"] == "guardian" and user["linked_user_id"] else user["id"]
RELATION_PERIOD_DAYS = 7
GUARDIAN_OVERVIEW_DAYS = 7
GUARDIAN_INACTIVE_HOURS = 48
GUARDIAN_ACTIVITY_DROP_DIVISOR = 2
GUARDIAN_HARD_MOOD_CHECK_COUNT = 3
ENGAGEMENT_COMPLETED_POINTS = 20
ENGAGEMENT_COMPLETED_MAX = 3
ENGAGEMENT_SHARED_POINTS = 20
ENGAGEMENT_SHARED_MAX = 2
RELATION_PENDING_BASIS = "대화 분석 AI 연결 후 제공돼요"
RELATION_METRICS = [("balance", "발화 균형"), ("response", "응답 반응"), ("empathy", "정서 동조"), ("engagement", "공동 참여"), ("recovery", "긴장 회복"), ("stability", "대화 안정")]
def relation_date_range():
    end = datetime.now(KOREA).date()
    return (end - timedelta(days=RELATION_PERIOD_DAYS - 1)).isoformat(), end.isoformat()
def relation_report(user):
    period_start, period_end = relation_date_range(); has_linked_senior = user["role"] != "guardian" or bool(user["linked_user_id"]); owner = activity_owner(user); completed_count = shared_count = 0
    if has_linked_senior:
        with get_db() as conn:
            row = conn.execute("""SELECT COUNT(*) AS completed_count, COALESCE(SUM(CASE WHEN is_shared = 1 THEN 1 ELSE 0 END), 0) AS shared_count FROM quest_progress WHERE user_id = ? AND completed = 1 AND date(completed_at) BETWEEN ? AND ?""", (owner, period_start, period_end)).fetchone()
        completed_count, shared_count = row["completed_count"], row["shared_count"]
    engagement_score = min(completed_count, ENGAGEMENT_COMPLETED_MAX) * ENGAGEMENT_COMPLETED_POINTS + min(shared_count, ENGAGEMENT_SHARED_MAX) * ENGAGEMENT_SHARED_POINTS
    metrics = [{"key": key, "label": label, "score": engagement_score, "status": "ready", "basis": f"최근 {RELATION_PERIOD_DAYS}일 관계 퀘스트 {completed_count}개 완료, 가족 공유 {shared_count}개"} if key == "engagement" and has_linked_senior else {"key": key, "label": label, "score": None, "status": "pending", "basis": RELATION_PENDING_BASIS} for key, label in RELATION_METRICS]
    ready_scores = [metric["score"] for metric in metrics if metric["status"] == "ready" and metric["score"] is not None]
    if engagement_score >= 70: insight_title, insight_body = "이번 주 가족과 꾸준히 함께했어요", "지금처럼 퀘스트를 이어가 보세요."
    elif engagement_score >= 40: insight_title, insight_body = "가족과 함께한 시간이 조금 더 필요해요", "오늘 퀘스트 하나를 가족과 나눠 보세요."
    else: insight_title, insight_body = "이번 주는 함께한 활동이 적었어요", "부담 없는 퀘스트부터 하나 시작해 보세요."
    return {"metrics": metrics, "overall_score": round(sum(ready_scores) / len(ready_scores)) if len(ready_scores) >= 3 else None, "duration_seconds": None, "confidence": None, "period_start": period_start, "period_end": period_end, "insight_title": insight_title, "insight_body": insight_body, "is_sample": False, "measured_at": now()}
def progress_dict(row): return dict(row) | {"completed": bool(row["completed"]), "is_shared": bool(row["is_shared"])}
def allowed_post(post_id: int, user, conn):
    row = conn.execute("SELECT * FROM timeline_posts WHERE id = ? AND deleted_at IS NULL", (post_id,)).fetchone()
    if not row: raise HTTPException(status_code=404, detail="게시물을 찾을 수 없습니다.")
    if row["author_id"] not in (user["id"], user["linked_user_id"]): raise HTTPException(status_code=403, detail="연결된 가족의 게시물만 볼 수 있습니다.")
    return row
def post_dict(row, user_id, conn):
    post_id = row["id"]
    counts = conn.execute("SELECT (SELECT COUNT(*) FROM post_likes WHERE post_id = ?) AS likes, (SELECT COUNT(*) FROM post_comments WHERE post_id = ? AND deleted_at IS NULL) AS comments, EXISTS(SELECT 1 FROM post_likes WHERE post_id = ? AND user_id = ?) AS liked", (post_id, post_id, post_id, user_id)).fetchone()
    media = conn.execute("SELECT id, content_type FROM post_media WHERE post_id = ? ORDER BY id", (post_id,)).fetchall()
    return dict(row) | {"is_mine": row["author_id"] == user_id, "like_count": counts["likes"], "comment_count": counts["comments"], "liked_by_me": bool(counts["liked"]), "media": [dict(item) for item in media]}
class SignUpInput(BaseModel): name: str = Field(min_length=1, max_length=30); email: str = Field(min_length=3, max_length=100); password: str = Field(min_length=4, max_length=100); role: str; partner_code: str = ""
class LoginInput(BaseModel): email: str; password: str
class LinkInput(BaseModel): partner_code: str
class ActivityInput(BaseModel): activity_type: str; question: str; answer: str; correct_answer: str; is_correct: bool; encouragement: str
class QuestInput(BaseModel): title: str; completed: bool; reflection: str = ""
class QuestProgressInput(BaseModel): quest_key: str; title: str; reflection: str = ""; result_summary: str = ""; is_shared: bool = False; media_kind: str = ""; media_count: int = 0
class AppStateInput(BaseModel): mode: str = "home"; step: int = 0
class MoodInput(BaseModel): mood: str
class MeetupInterestInput(BaseModel): meetup_key: str = Field(min_length=1, max_length=80)
class CommentInput(BaseModel): body: str = Field(min_length=1, max_length=300)
class ArticleInput(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    summary: str = Field(default="", max_length=200)
    body: str = Field(min_length=1, max_length=10000)
    category: str
    source_name: str = Field(default="", max_length=100)
    source_url: str = Field(default="", max_length=1000)
    status: str
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
        try: cursor = conn.execute("INSERT INTO users (name,email,password_hash,role,invite_code,linked_user_id,created_at) VALUES (?,?,?,?,?,?,?)", (payload.name.strip(), payload.email.strip().lower(), password_hash(payload.password), payload.role, make_code(conn), linked_id, now()))
        except sqlite3.IntegrityError: raise HTTPException(status_code=400, detail="이미 사용 중인 이메일입니다.")
        user_id = cursor.lastrowid
        if linked_id: conn.execute("UPDATE users SET linked_user_id = ? WHERE id = ?", (user_id, linked_id))
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone(); token = secrets.token_urlsafe(32); conn.execute("INSERT INTO sessions (token,user_id,created_at) VALUES (?,?,?)", (token, user_id, now()))
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
        conn.execute("UPDATE users SET linked_user_id = ? WHERE id = ?", (partner["id"], user["id"])); conn.execute("UPDATE users SET linked_user_id = ? WHERE id = ?", (user["id"], partner["id"])); updated = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    return {"user": profile(updated)}
@app.post("/api/auth/logout")
def logout(authorization: Optional[str] = Header(default=None)):
    if authorization and authorization.startswith("Bearer "):
        with get_db() as conn: conn.execute("DELETE FROM sessions WHERE token = ?", (authorization.removeprefix("Bearer "),))
    return {"ok": True}
@app.get("/api/meetups/interests")
def meetup_interests(user=Depends(current_user)):
    with get_db() as conn: rows = conn.execute("SELECT meetup_key FROM meetup_interests WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)).fetchall()
    return [row["meetup_key"] for row in rows]
@app.post("/api/meetups/interests")
def add_meetup_interest(payload: MeetupInterestInput, user=Depends(current_user)):
    if user["role"] != "senior": raise HTTPException(status_code=403, detail="시니어 계정에서만 관심 등록을 할 수 있습니다.")
    with get_db() as conn: conn.execute("INSERT OR IGNORE INTO meetup_interests (user_id,meetup_key,created_at) VALUES (?,?,?)", (user["id"], payload.meetup_key, now()))
    return {"meetup_key": payload.meetup_key}
@app.delete("/api/meetups/interests/{meetup_key}")
def remove_meetup_interest(meetup_key: str, user=Depends(current_user)):
    if user["role"] != "senior": raise HTTPException(status_code=403, detail="시니어 계정에서만 관심 취소를 할 수 있습니다.")
    with get_db() as conn: conn.execute("DELETE FROM meetup_interests WHERE user_id = ? AND meetup_key = ?", (user["id"], meetup_key))
    return {"meetup_key": meetup_key}
@app.get("/api/mood/today")
def mood_today(user=Depends(current_user)):
    date = today_korea()
    with get_db() as conn: row = conn.execute("SELECT mood, checkin_date, updated_at FROM mood_checkins WHERE user_id = ? AND checkin_date = ?", (user["id"], date)).fetchone()
    return dict(row) if row else {"mood": None, "checkin_date": date, "updated_at": None}
@app.post("/api/mood")
def save_mood(payload: MoodInput, user=Depends(current_user)):
    if user["role"] != "senior": raise HTTPException(status_code=403, detail="시니어 계정에서만 기분을 기록할 수 있습니다.")
    if payload.mood not in ("good", "okay", "hard"): raise HTTPException(status_code=400, detail="기분을 다시 선택해 주세요.")
    date, timestamp = today_korea(), now()
    with get_db() as conn:
        conn.execute("""INSERT INTO mood_checkins (user_id,mood,checkin_date,created_at,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(user_id,checkin_date) DO UPDATE SET mood=excluded.mood, updated_at=excluded.updated_at""", (user["id"], payload.mood, date, timestamp, timestamp)); row = conn.execute("SELECT mood, checkin_date, updated_at FROM mood_checkins WHERE user_id = ? AND checkin_date = ?", (user["id"], date)).fetchone()
    return dict(row)
@app.get("/api/mood/recent")
def recent_moods(days: int = 7, user=Depends(current_user)):
    safe_days = min(max(days, 1), 30); owner = activity_owner(user); date = today_korea()
    with get_db() as conn: rows = conn.execute("SELECT mood, checkin_date FROM mood_checkins WHERE user_id = ? AND checkin_date >= date(?, ?) ORDER BY checkin_date DESC", (owner, date, f"-{safe_days - 1} days")).fetchall()
    return [dict(row) for row in rows]
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
        cursor = conn.execute("INSERT INTO activities (activity_type,question,answer,correct_answer,is_correct,encouragement,completed_at,user_id) VALUES (?,?,?,?,?,?,?,?)", (payload.activity_type,payload.question,payload.answer,payload.correct_answer,int(payload.is_correct),payload.encouragement,now(),user["id"])); row = conn.execute("SELECT * FROM activities WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row) | {"is_correct": bool(row["is_correct"])}
@app.get("/api/quests")
def list_quests(user=Depends(current_user)):
    with get_db() as conn: rows = conn.execute("SELECT * FROM quests WHERE user_id = ? ORDER BY COALESCE(completed_at, '') DESC, id DESC", (activity_owner(user),)).fetchall()
    return [dict(row) | {"completed": bool(row["completed"])} for row in rows]
@app.post("/api/quests")
def create_quest(payload: QuestInput, user=Depends(current_user)):
    if user["role"] != "senior": raise HTTPException(status_code=403, detail="시니어 계정에서만 퀘스트를 기록할 수 있습니다.")
    with get_db() as conn:
        cursor = conn.execute("INSERT INTO quests (title,completed,reflection,completed_at,user_id) VALUES (?,?,?,?,?)", (payload.title,int(payload.completed),payload.reflection.strip(),now() if payload.completed else None,user["id"])); row = conn.execute("SELECT * FROM quests WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row) | {"completed": bool(row["completed"])}
@app.get("/api/quest-progress")
def quest_progress(user=Depends(current_user)):
    with get_db() as conn: rows = conn.execute("SELECT * FROM quest_progress WHERE user_id = ? ORDER BY completed_at DESC", (user["id"],)).fetchall()
    return [progress_dict(row) for row in rows]
@app.get("/api/family-timeline")
def family_timeline(user=Depends(current_user)):
    ids = [user["id"]] + ([user["linked_user_id"]] if user["linked_user_id"] else []); marks = ",".join("?" for _ in ids)
    with get_db() as conn: rows = conn.execute(f"SELECT q.*, u.name AS author_name FROM quest_progress q JOIN users u ON u.id=q.user_id WHERE q.user_id = ? OR (q.user_id IN ({marks}) AND q.is_shared = 1) ORDER BY q.completed_at DESC", [user["id"], *ids]).fetchall()
    return [progress_dict(row) | {"is_mine": row["user_id"] == user["id"]} for row in rows]
@app.post("/api/quest-progress")
def complete_quest(payload: QuestProgressInput, user=Depends(current_user)):
    timestamp = now()
    with get_db() as conn:
        conn.execute("""INSERT INTO quest_progress (user_id,quest_key,title,role,completed,reflection,result_summary,completed_at,is_shared,media_kind,media_count) VALUES (?,?,?,?,1,?,?,?,?,?,?) ON CONFLICT(user_id,quest_key) DO UPDATE SET completed=1, reflection=excluded.reflection, result_summary=excluded.result_summary, completed_at=excluded.completed_at, is_shared=excluded.is_shared, media_kind=excluded.media_kind, media_count=excluded.media_count""", (user["id"],payload.quest_key,payload.title,user["role"],payload.reflection.strip(),payload.result_summary.strip(),timestamp,int(payload.is_shared),payload.media_kind,max(0,payload.media_count)))
        row = conn.execute("SELECT * FROM quest_progress WHERE user_id = ? AND quest_key = ?", (user["id"], payload.quest_key)).fetchone()
        post_id = None
        if payload.is_shared: post_id = conn.execute("INSERT INTO timeline_posts (author_id,quest_key,title,body,result_summary,created_at) VALUES (?,?,?,?,?,?)", (user["id"], payload.quest_key, payload.title, payload.reflection.strip(), payload.result_summary.strip(), timestamp)).lastrowid
    return progress_dict(row) | {"post_id": post_id}
@app.get("/api/timeline")
def timeline(before_id: Optional[int] = None, limit: int = 20, user=Depends(current_user)):
    safe_limit = min(max(limit, 1), 20); ids = [user["id"]] + ([user["linked_user_id"]] if user["linked_user_id"] else []); marks = ",".join("?" for _ in ids)
    sql = f"SELECT p.*, u.name AS author_name, u.role AS author_role FROM timeline_posts p JOIN users u ON u.id=p.author_id WHERE p.deleted_at IS NULL AND p.author_id IN ({marks})"
    params = list(ids)
    if before_id: sql += " AND p.id < ?"; params.append(before_id)
    sql += " ORDER BY p.id DESC LIMIT ?"; params.append(safe_limit)
    with get_db() as conn: rows = conn.execute(sql, params).fetchall(); return [post_dict(row, user["id"], conn) for row in rows]
@app.get("/api/timeline/{post_id}")
def timeline_detail(post_id: int, user=Depends(current_user)):
    with get_db() as conn:
        post = allowed_post(post_id, user, conn); result = post_dict(conn.execute("SELECT p.*, u.name AS author_name, u.role AS author_role FROM timeline_posts p JOIN users u ON u.id=p.author_id WHERE p.id = ?", (post_id,)).fetchone(), user["id"], conn)
        comments = conn.execute("SELECT c.id,c.user_id,c.body,c.created_at,u.name AS author_name,u.role AS author_role FROM post_comments c JOIN users u ON u.id=c.user_id WHERE c.post_id = ? AND c.deleted_at IS NULL ORDER BY c.id ASC", (post_id,)).fetchall()
    return result | {"comments": [dict(item) | {"is_mine": item["user_id"] == user["id"]} for item in comments]}
@app.post("/api/timeline/{post_id}/like")
def add_like(post_id: int, user=Depends(current_user)):
    with get_db() as conn: allowed_post(post_id, user, conn); conn.execute("INSERT OR IGNORE INTO post_likes (post_id,user_id,created_at) VALUES (?,?,?)", (post_id,user["id"],now()))
    return {"ok": True}
@app.delete("/api/timeline/{post_id}/like")
def remove_like(post_id: int, user=Depends(current_user)):
    with get_db() as conn: allowed_post(post_id, user, conn); conn.execute("DELETE FROM post_likes WHERE post_id = ? AND user_id = ?", (post_id,user["id"]))
    return {"ok": True}
@app.post("/api/timeline/{post_id}/comments")
def add_comment(post_id: int, payload: CommentInput, user=Depends(current_user)):
    body = payload.body.strip()
    if not body: raise HTTPException(status_code=400, detail="댓글을 입력해 주세요.")
    with get_db() as conn:
        allowed_post(post_id, user, conn); cursor = conn.execute("INSERT INTO post_comments (post_id,user_id,body,created_at) VALUES (?,?,?,?)", (post_id,user["id"],body,now())); row = conn.execute("SELECT c.id,c.user_id,c.body,c.created_at,u.name AS author_name,u.role AS author_role FROM post_comments c JOIN users u ON u.id=c.user_id WHERE c.id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row) | {"is_mine": True}
@app.delete("/api/timeline/comments/{comment_id}")
def delete_comment(comment_id: int, user=Depends(current_user)):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM post_comments WHERE id = ? AND deleted_at IS NULL", (comment_id,)).fetchone()
        if not row: raise HTTPException(status_code=404, detail="댓글을 찾을 수 없습니다.")
        if row["user_id"] != user["id"]: raise HTTPException(status_code=403, detail="내 댓글만 삭제할 수 있습니다.")
        conn.execute("UPDATE post_comments SET deleted_at = ? WHERE id = ?", (now(),comment_id))
    return {"ok": True}
@app.delete("/api/timeline/{post_id}")
def delete_post(post_id: int, user=Depends(current_user)):
    with get_db() as conn:
        row = allowed_post(post_id, user, conn)
        if row["author_id"] != user["id"]: raise HTTPException(status_code=403, detail="내 게시물만 삭제할 수 있습니다.")
        conn.execute("UPDATE timeline_posts SET deleted_at = ? WHERE id = ?", (now(),post_id))
    return {"ok": True}
def image_type(data: bytes):
    if data.startswith(b"\xff\xd8\xff"): return "image/jpeg", ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"): return "image/png", ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP": return "image/webp", ".webp"
    return None, None
@app.post("/api/timeline/{post_id}/media")
async def upload_media(post_id: int, request: Request, user=Depends(current_user)):
    # 사진 파일 자체를 요청 본문으로 받습니다. (추가 패키지 없이 동작)
    with get_db() as conn:
        post = allowed_post(post_id, user, conn)
        if post["author_id"] != user["id"]: raise HTTPException(status_code=403, detail="내 게시물에만 사진을 올릴 수 있습니다.")
        if conn.execute("SELECT COUNT(*) FROM post_media WHERE post_id = ?", (post_id,)).fetchone()[0] >= MEDIA_MAX_PER_POST:
            raise HTTPException(status_code=400, detail=f"사진은 게시물당 {MEDIA_MAX_PER_POST}장까지 올릴 수 있어요.")
    data = b""
    async for chunk in request.stream():
        data += chunk
        if len(data) > MEDIA_MAX_BYTES: raise HTTPException(status_code=400, detail="사진은 한 장에 5MB까지 올릴 수 있어요.")
    content_type, ext = image_type(data)
    if not content_type: raise HTTPException(status_code=400, detail="jpg, png, webp 사진만 올릴 수 있어요.")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    file_name = secrets.token_hex(16) + ext
    with open(os.path.join(UPLOAD_DIR, file_name), "wb") as f: f.write(data)
    with get_db() as conn:
        media_id = conn.execute("INSERT INTO post_media (post_id,user_id,file_name,content_type,size_bytes,created_at) VALUES (?,?,?,?,?,?)", (post_id, user["id"], file_name, content_type, len(data), now())).lastrowid
    return {"id": media_id, "content_type": content_type}
@app.get("/api/media/{media_id}")
def get_media(media_id: int, user=Depends(current_user)):
    with get_db() as conn:
        media = conn.execute("SELECT * FROM post_media WHERE id = ?", (media_id,)).fetchone()
        if not media: raise HTTPException(status_code=404, detail="사진을 찾을 수 없습니다.")
        allowed_post(media["post_id"], user, conn)
    path = os.path.join(UPLOAD_DIR, os.path.basename(media["file_name"]))
    if not os.path.exists(path): raise HTTPException(status_code=404, detail="사진 파일을 찾을 수 없습니다.")
    return FileResponse(path, media_type=media["content_type"], headers={"Cache-Control": "private, max-age=86400"})
ARTICLE_CATEGORIES = ("인지 건강", "생활 습관", "대화 팁", "공지")
def article_dict(row): return dict(row)
def article_payload(payload: ArticleInput):
    title, summary, body = payload.title.strip(), payload.summary.strip(), payload.body.strip()
    category, source_name, source_url = payload.category.strip(), payload.source_name.strip(), payload.source_url.strip()
    if not title or not body: raise HTTPException(status_code=400, detail="제목과 본문을 입력해 주세요.")
    if category not in ARTICLE_CATEGORIES: raise HTTPException(status_code=400, detail="분류를 다시 선택해 주세요.")
    if payload.status not in ("draft", "published"): raise HTTPException(status_code=400, detail="저장 상태를 다시 선택해 주세요.")
    if source_url and not (source_url.startswith("http://") or source_url.startswith("https://")): raise HTTPException(status_code=400, detail="출처 링크는 http:// 또는 https://로 시작해야 합니다.")
    return title, summary, body, category, source_name, source_url, payload.status
@app.get("/api/articles")
def articles(category: Optional[str] = None, before_id: Optional[int] = None, limit: int = 20, user=Depends(current_user)):
    safe_limit = min(max(limit, 1), 20); sql = "SELECT * FROM articles WHERE status = 'published'"; params = []
    if category and category != "전체":
        if category not in ARTICLE_CATEGORIES: raise HTTPException(status_code=400, detail="분류를 다시 선택해 주세요.")
        sql += " AND category = ?"; params.append(category)
    if before_id: sql += " AND id < ?"; params.append(before_id)
    sql += " ORDER BY published_at DESC, id DESC LIMIT ?"; params.append(safe_limit)
    with get_db() as conn: rows = conn.execute(sql, params).fetchall()
    return [article_dict(row) for row in rows]
@app.get("/api/articles/{article_id}")
def article_detail(article_id: int, user=Depends(current_user)):
    with get_db() as conn: row = conn.execute("SELECT * FROM articles WHERE id = ? AND status = 'published'", (article_id,)).fetchone()
    if not row: raise HTTPException(status_code=404, detail="글을 찾을 수 없습니다.")
    return article_dict(row)
@app.get("/api/admin/articles")
def admin_articles(user=Depends(require_admin)):
    with get_db() as conn: rows = conn.execute("SELECT * FROM articles ORDER BY CASE status WHEN 'draft' THEN 0 ELSE 1 END, updated_at DESC, id DESC").fetchall()
    return [article_dict(row) for row in rows]
@app.post("/api/admin/articles")
def create_article(payload: ArticleInput, user=Depends(require_admin)):
    title, summary, body, category, source_name, source_url, status = article_payload(payload); timestamp = now(); published_at = timestamp if status == "published" else None
    with get_db() as conn:
        cursor = conn.execute("INSERT INTO articles (title,summary,body,category,source_name,source_url,author_id,status,created_at,updated_at,published_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (title,summary,body,category,source_name,source_url,user["id"],status,timestamp,timestamp,published_at)); row = conn.execute("SELECT * FROM articles WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return article_dict(row)
@app.put("/api/admin/articles/{article_id}")
def update_article(article_id: int, payload: ArticleInput, user=Depends(require_admin)):
    title, summary, body, category, source_name, source_url, status = article_payload(payload); timestamp = now()
    with get_db() as conn:
        existing = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
        if not existing: raise HTTPException(status_code=404, detail="글을 찾을 수 없습니다.")
        published_at = existing["published_at"] or timestamp if status == "published" else None
        conn.execute("UPDATE articles SET title=?,summary=?,body=?,category=?,source_name=?,source_url=?,status=?,updated_at=?,published_at=? WHERE id=?", (title,summary,body,category,source_name,source_url,status,timestamp,published_at,article_id)); row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
    return article_dict(row)
@app.delete("/api/admin/articles/{article_id}")
def remove_article(article_id: int, user=Depends(require_admin)):
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
        if cursor.rowcount == 0: raise HTTPException(status_code=404, detail="글을 찾을 수 없습니다.")
    return {"ok": True}
@app.get("/api/guardian/overview")
def guardian_overview(user=Depends(current_user)):
    if user["role"] != "guardian": raise HTTPException(status_code=403, detail="보호자 계정에서만 볼 수 있습니다.")
    if not user["linked_user_id"]:
        return {"senior": {"name": None, "linked": False, "last_activity_at": None}, "status": "unknown", "signals": [], "recommendation": {"title": "연결된 시니어를 기다리고 있어요", "body": "연결 코드를 나누면 활동 기록을 함께 살펴볼 수 있어요.", "action": "none"}}
    end = datetime.now(KOREA).date(); start = end - timedelta(days=GUARDIAN_OVERVIEW_DAYS - 1); previous_start = start - timedelta(days=GUARDIAN_OVERVIEW_DAYS); previous_end = start - timedelta(days=1); senior_id = user["linked_user_id"]
    def period_count(conn, table, column, user_id, begin, finish):
        return conn.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE {column} = ? AND date(created_at) BETWEEN ? AND ?", (user_id, begin.isoformat(), finish.isoformat())).fetchone()["count"]
    with get_db() as conn:
        senior = conn.execute("SELECT name FROM users WHERE id = ?", (senior_id,)).fetchone()
        activity_now = conn.execute("SELECT COUNT(*) AS count, COALESCE(SUM(is_correct),0) AS correct FROM activities WHERE user_id = ? AND date(completed_at) BETWEEN ? AND ?", (senior_id, start.isoformat(), end.isoformat())).fetchone()
        activity_prev = conn.execute("SELECT COUNT(*) AS count FROM activities WHERE user_id = ? AND date(completed_at) BETWEEN ? AND ?", (senior_id, previous_start.isoformat(), previous_end.isoformat())).fetchone()["count"]
        moods = conn.execute("SELECT mood, COUNT(*) AS count FROM mood_checkins WHERE user_id = ? AND checkin_date BETWEEN ? AND ? GROUP BY mood", (senior_id, start.isoformat(), end.isoformat())).fetchall(); mood_counts = {row["mood"]: row["count"] for row in moods}
        family_ids = (senior_id, user["id"]); marks = ",".join("?" for _ in family_ids)
        shared_now = conn.execute(f"SELECT COUNT(*) AS count FROM timeline_posts WHERE author_id IN ({marks}) AND deleted_at IS NULL AND date(created_at) BETWEEN ? AND ?", (*family_ids, start.isoformat(), end.isoformat())).fetchone()["count"]
        comments_now = conn.execute(f"SELECT COUNT(*) AS count FROM post_comments WHERE user_id IN ({marks}) AND deleted_at IS NULL AND date(created_at) BETWEEN ? AND ?", (*family_ids, start.isoformat(), end.isoformat())).fetchone()["count"]
        likes_now = conn.execute(f"SELECT COUNT(*) AS count FROM post_likes WHERE user_id IN ({marks}) AND date(created_at) BETWEEN ? AND ?", (*family_ids, start.isoformat(), end.isoformat())).fetchone()["count"]
        previous_social = conn.execute(f"SELECT (SELECT COUNT(*) FROM timeline_posts WHERE author_id IN ({marks}) AND deleted_at IS NULL AND date(created_at) BETWEEN ? AND ?) + (SELECT COUNT(*) FROM post_comments WHERE user_id IN ({marks}) AND deleted_at IS NULL AND date(created_at) BETWEEN ? AND ?) + (SELECT COUNT(*) FROM post_likes WHERE user_id IN ({marks}) AND date(created_at) BETWEEN ? AND ?) AS count", (*family_ids, previous_start.isoformat(), previous_end.isoformat(), *family_ids, previous_start.isoformat(), previous_end.isoformat(), *family_ids, previous_start.isoformat(), previous_end.isoformat())).fetchone()["count"]
        latest_rows = []
        for table, field in (("activities", "completed_at"), ("quest_progress", "completed_at"), ("mood_checkins", "updated_at"), ("timeline_posts", "created_at")):
            row = conn.execute(f"SELECT MAX({field}) AS value FROM {table} WHERE {'author_id' if table == 'timeline_posts' else 'user_id'} = ?" + (" AND deleted_at IS NULL" if table == "timeline_posts" else ""), (senior_id,)).fetchone()
            if row["value"]: latest_rows.append(row["value"])
    current_activity, correct = activity_now["count"], activity_now["correct"]
    activity_status = "check" if current_activity == 0 else "attention" if activity_prev and current_activity * GUARDIAN_ACTIVITY_DROP_DIVISOR <= activity_prev else "stable"
    activity_title = "이번 주 활동 기록이 없어요" if current_activity == 0 else "활동 참여가 줄었어요" if activity_status == "attention" else "꾸준히 참여하고 있어요"
    accuracy = round(correct / current_activity * 100) if current_activity else 0
    hard = mood_counts.get("hard", 0); good = mood_counts.get("good", 0); okay = mood_counts.get("okay", 0)
    mood_status = "check" if hard >= GUARDIAN_HARD_MOOD_CHECK_COUNT else "attention" if hard or not (hard + good + okay) else "stable"
    mood_title = "힘든 날이 많았어요" if hard >= GUARDIAN_HARD_MOOD_CHECK_COUNT else "힘든 날이 있었어요" if hard else "기분 기록이 없어요" if not (hard + good + okay) else "편안한 한 주였어요"
    social_now = shared_now + comments_now + likes_now
    social_status = "attention" if social_now == 0 or social_now < previous_social else "stable"
    social_title = "이번 주 가족 소통이 없었어요" if social_now == 0 else "가족 소통이 줄었어요" if social_now < previous_social else "가족과 활발히 소통했어요"
    def trend(current, previous): return "up" if current > previous else "down" if current < previous else "same"
    signals = [
        {"key": "activity", "label": "활동 신호", "status": activity_status, "title": activity_title, "detail": f"이번 주 {current_activity}회 (지난주 {activity_prev}회) · 정답률 {accuracy}%", "trend": trend(current_activity, activity_prev)},
        {"key": "mood", "label": "기분 신호", "status": mood_status, "title": mood_title, "detail": f"좋아요 {good} · 보통 {okay} · 힘들어요 {hard}", "trend": None},
        {"key": "connection", "label": "연결 신호", "status": social_status, "title": social_title, "detail": f"공유 {shared_now} · 댓글 {comments_now} · 좋아요 {likes_now}", "trend": trend(social_now, previous_social)},
        {"key": "life", "label": "생활 신호", "status": "unknown", "title": "워치를 연결하면 표시돼요", "detail": "수면·걸음 정보는 워치 연결 후 확인할 수 있어요.", "trend": None},
    ]
    priority = {"stable": 0, "attention": 1, "check": 2}; concerning = max(signals[:3], key=lambda item: priority[item["status"]]); overall = concerning["status"]
    last_activity = max(latest_rows) if latest_rows else None
    if last_activity:
        try:
            if datetime.now(KOREA) - datetime.fromisoformat(last_activity).astimezone(KOREA) > timedelta(hours=GUARDIAN_INACTIVE_HOURS): overall = "check"
        except ValueError: pass
    recommendation_map = {
        "mood": {"title": "안부 전화를 해 보세요", "body": "이번 주 힘든 날이 있었어요. 10분 통화로 마음을 나눠 보세요.", "action": "call_quest"},
        "connection": {"title": "가족 소식에 반응해 주세요", "body": "공유한 기록에 댓글 하나가 큰 힘이 돼요.", "action": "timeline"},
        "activity": {"title": "함께 퀘스트를 해 보세요", "body": "부담 없는 퀘스트로 일상을 함께 나눠 보세요.", "action": "call_quest"},
    }
    recommendation = recommendation_map[concerning["key"] if concerning["status"] != "stable" else "activity"] if concerning["status"] != "stable" else {"title": "지금처럼 함께해 주세요", "body": "꾸준한 관심이 가장 큰 돌봄이에요.", "action": "none"}
    return {"senior": {"name": senior["name"] if senior else None, "linked": True, "last_activity_at": last_activity}, "status": overall, "signals": signals, "recommendation": recommendation}
@app.get("/api/report/relation")
def get_relation_report(user=Depends(current_user)): return relation_report(user)
@app.get("/api/summary")
def summary(user=Depends(current_user)):
    owner = activity_owner(user)
    with get_db() as conn: activities = conn.execute("SELECT * FROM activities WHERE user_id = ? ORDER BY completed_at DESC", (owner,)).fetchall(); quests = conn.execute("SELECT * FROM quests WHERE user_id = ?", (owner,)).fetchall(); progress = conn.execute("SELECT * FROM quest_progress WHERE user_id = ?", (user["id"],)).fetchall()
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
    with get_db() as conn: rows = conn.execute("SELECT state_key,state_value FROM user_app_state WHERE user_id = ?", (user["id"],)).fetchall()
    values = {row["state_key"]: row["state_value"] for row in rows}; return {"mode": values.get("mode", "home"), "step": int(values.get("step", "0"))}
@app.put("/api/app-state")
def save_app_state(payload: AppStateInput, user=Depends(current_user)):
    with get_db() as conn:
        for key, value in (("mode",payload.mode),("step",str(max(0,payload.step)))):
            conn.execute("INSERT OR REPLACE INTO user_app_state (user_id,state_key,state_value,updated_at) VALUES (?,?,?,?)", (user["id"],key,value,now()))
    return {"mode": payload.mode, "step": max(0,payload.step)}
@app.get("/api/guardian/report")
def guardian_report(days: int = 7, user=Depends(current_user)):
    if user["role"] != "guardian": raise HTTPException(status_code=403, detail="보호자 계정에서만 볼 수 있습니다.")
    if not user["linked_user_id"]: raise HTTPException(status_code=400, detail="연결된 시니어가 있어야 리포트를 볼 수 있습니다.")
    safe_days = 7 if days == 7 else 30
    end = datetime.now(KOREA).date(); start = end - timedelta(days=safe_days - 1); senior_id = user["linked_user_id"]
    week_starts = []
    cursor = start - timedelta(days=start.weekday())
    while cursor <= end:
        week_starts.append(cursor)
        cursor += timedelta(days=7)
    def week_label(value): return value - timedelta(days=value.weekday())
    with get_db() as conn:
        activities = conn.execute("SELECT activity_type,is_correct,completed_at FROM activities WHERE user_id = ? AND date(completed_at) BETWEEN ? AND ?", (senior_id, start.isoformat(), end.isoformat())).fetchall()
        moods = conn.execute("SELECT checkin_date,mood FROM mood_checkins WHERE user_id = ? AND checkin_date BETWEEN ? AND ? ORDER BY checkin_date", (senior_id, start.isoformat(), end.isoformat())).fetchall()
        progress = conn.execute("SELECT user_id,completed_at FROM quest_progress WHERE user_id IN (?,?) AND completed = 1 AND date(completed_at) BETWEEN ? AND ?", (senior_id, user["id"], start.isoformat(), end.isoformat())).fetchall()
        family_ids = (senior_id, user["id"]); marks = ",".join("?" for _ in family_ids)
        shared = conn.execute(f"SELECT COUNT(*) AS count FROM timeline_posts WHERE author_id IN ({marks}) AND deleted_at IS NULL AND date(created_at) BETWEEN ? AND ?", (*family_ids, start.isoformat(), end.isoformat())).fetchone()["count"]
        comments = conn.execute(f"SELECT COUNT(*) AS count FROM post_comments WHERE user_id IN ({marks}) AND deleted_at IS NULL AND date(created_at) BETWEEN ? AND ?", (*family_ids, start.isoformat(), end.isoformat())).fetchone()["count"]
        likes = conn.execute(f"SELECT COUNT(*) AS count FROM post_likes WHERE user_id IN ({marks}) AND date(created_at) BETWEEN ? AND ?", (*family_ids, start.isoformat(), end.isoformat())).fetchone()["count"]
    activity_buckets = {}; accuracy_buckets = {}
    for row in activities:
        week, activity_type = week_label(datetime.fromisoformat(row["completed_at"]).astimezone(KOREA).date()).isoformat(), row["activity_type"]
        bucket = activity_buckets.setdefault((week, activity_type), [0, 0]); bucket[0] += 1; bucket[1] += int(row["is_correct"] or 0)
        total = accuracy_buckets.setdefault(activity_type, [0, 0]); total[0] += 1; total[1] += int(row["is_correct"] or 0)
    activity_by_week = [{"week_start": week, "type": activity_type, "count": value[0], "accuracy": round(value[1] / value[0] * 100) if value[0] else 0} for (week, activity_type), value in sorted(activity_buckets.items())]
    activity_accuracy = [{"type": activity_type, "count": value[0], "accuracy": round(value[1] / value[0] * 100) if value[0] else 0} for activity_type, value in sorted(accuracy_buckets.items())]
    quest_buckets = {week.isoformat(): {"week_start": week.isoformat(), "senior_count": 0, "guardian_count": 0} for week in week_starts}
    for row in progress:
        week = week_label(datetime.fromisoformat(row["completed_at"]).astimezone(KOREA).date()).isoformat()
        if week in quest_buckets: quest_buckets[week]["senior_count" if row["user_id"] == senior_id else "guardian_count"] += 1
    activity_total = len(activities); hard_count = sum(1 for row in moods if row["mood"] == "hard")
    recommendations = []
    if activity_total == 0: recommendations.append({"title": "부담 없는 퀘스트를 함께 시작해 보세요", "reason": "선택한 기간에 인지 활동 기록이 없어요.", "action": "quest"})
    elif hard_count: recommendations.append({"title": "안부를 천천히 물어봐 주세요", "reason": f"선택한 기간에 힘들어요 기록이 {hard_count}일 있어요.", "action": "call_quest"})
    if shared + comments + likes == 0: recommendations.append({"title": "가족 소식에 짧은 댓글을 남겨 보세요", "reason": "선택한 기간에 가족 소통 기록이 없어요.", "action": "timeline"})
    if not recommendations: recommendations.append({"title": "지금처럼 함께해 주세요", "reason": "활동과 가족 소통 기록이 꾸준히 이어지고 있어요.", "action": "none"})
    if len(recommendations) < 3: recommendations.append({"title": "완료한 퀘스트를 함께 돌아봐 주세요", "reason": "작은 성취를 이야기로 나누면 다음 활동을 이어가기 좋아요.", "action": "quest"})
    if len(recommendations) < 3: recommendations.append({"title": "가족 소식에 마음을 남겨 보세요", "reason": "좋아요나 댓글 하나로 함께한 기록에 반응할 수 있어요.", "action": "timeline"})
    return {"period_start": start.isoformat(), "period_end": end.isoformat(), "activity_by_week": activity_by_week, "activity_accuracy": activity_accuracy, "mood_by_day": [{"date": row["checkin_date"], "mood": row["mood"]} for row in moods], "quests_by_week": list(quest_buckets.values()), "interactions": {"shared": shared, "comments": comments, "likes": likes}, "recommendations": recommendations[:3]}
