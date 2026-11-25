import os
import sqlite3
import secrets
import hashlib
import re
import smtplib
import time
from email.message import EmailMessage
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
AUDIO_MAX_BYTES = 10 * 1024 * 1024  # 녹음은 약 10분 분량까지
QUEST_DAILY_LIMIT = 3  # 하루에 완료할 수 있는 퀘스트 수
# 메일 발송 설정 (Gmail/네이버 메일의 앱 비밀번호). 비어 있으면 이메일 인증 없이 가입됩니다.
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "") or SMTP_USER
MAIL_DEBUG = os.environ.get("MAIL_DEBUG", "") == "1"  # 시험용: 메일 대신 서버 로그에 코드를 남깁니다.
CODE_TTL_SECONDS = 10 * 60
CODE_MAX_ATTEMPTS = 5
CODE_RESEND_SECONDS = 60
CODE_MAX_PER_HOUR = 5
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
        # 퀘스트를 완료할 때마다 한 줄씩 쌓는 기록 (quest_progress는 퀘스트당 한 줄이라 다시 하면 덮어써짐)
        conn.execute("""CREATE TABLE IF NOT EXISTS quest_completions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, quest_key TEXT NOT NULL,
            round INTEGER NOT NULL DEFAULT 1, completed_at TEXT NOT NULL, kdate TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS email_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, purpose TEXT NOT NULL, code_hash TEXT NOT NULL,
            expires_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, used_at INTEGER, created_at INTEGER NOT NULL)""")
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
            ("activities", "response_ms", "INTEGER"),
            ("timeline_posts", "source_progress_id", "INTEGER UNIQUE"),
            ("users", "email_verified", "INTEGER NOT NULL DEFAULT 0"),
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
        # 예전에 공유된 기록 중 타임라인에 아직 없는 것만 옮깁니다.
        # (이미 같은 글쓴이·퀘스트·시각의 게시물이 있으면 건너뜀 — 재시작할 때마다 중복되던 문제 수정)
        rows = conn.execute("""SELECT q.* FROM quest_progress q WHERE q.is_shared = 1 AND q.completed = 1
            AND NOT EXISTS (SELECT 1 FROM timeline_posts p WHERE p.author_id = q.user_id AND p.quest_key = q.quest_key AND p.created_at = q.completed_at)""").fetchall()
        for row in rows:
            conn.execute("""INSERT OR IGNORE INTO timeline_posts
                (author_id,quest_key,title,body,result_summary,created_at,source_progress_id)
                VALUES (?,?,?,?,?,?,?)""", (row["user_id"], row["quest_key"], row["title"], row["reflection"] or "", row["result_summary"] or "", row["completed_at"] or now(), row["id"]))
def remove_duplicate_posts():
    # 위 문제로 이미 생긴 중복 게시물 정리: 댓글·좋아요·사진을 원래 게시물로 옮긴 뒤 복사본을 지웁니다.
    with get_db() as conn:
        pairs = conn.execute("""SELECT d.id AS dup, MIN(o.id) AS orig FROM timeline_posts d JOIN timeline_posts o
            ON o.author_id = d.author_id AND o.quest_key = d.quest_key AND o.created_at = d.created_at AND o.id != d.id AND o.source_progress_id IS NULL
            WHERE d.source_progress_id IS NOT NULL GROUP BY d.id""").fetchall()
        for pair in pairs:
            dup, orig = pair["dup"], pair["orig"]
            conn.execute("UPDATE post_media SET post_id = ? WHERE post_id = ?", (orig, dup))
            conn.execute("UPDATE post_comments SET post_id = ? WHERE post_id = ?", (orig, dup))
            conn.execute("UPDATE OR IGNORE post_likes SET post_id = ? WHERE post_id = ?", (orig, dup))
            conn.execute("DELETE FROM post_likes WHERE post_id = ?", (dup,))
            conn.execute("DELETE FROM timeline_posts WHERE id = ?", (dup,))
remove_duplicate_posts()
migrate_shared_progress()
def migrate_completions():
    with get_db() as conn:
        rows = conn.execute("""SELECT q.user_id, q.quest_key, q.completed_at FROM quest_progress q WHERE q.completed = 1 AND q.completed_at IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM quest_completions c WHERE c.user_id = q.user_id AND c.quest_key = q.quest_key)""").fetchall()
        for row in rows:
            conn.execute("INSERT INTO quest_completions (user_id,quest_key,round,completed_at,kdate) VALUES (?,?,1,?,?)", (row["user_id"], row["quest_key"], row["completed_at"], row["completed_at"][:10]))
migrate_completions()
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
    return {"id": row["id"], "name": row["name"], "email": row["email"], "role": row["role"], "invite_code": row["invite_code"], "linked_user_id": row["linked_user_id"], "is_linked": bool(row["linked_user_id"]), "is_admin": is_admin(row), "email_verified": bool(row["email_verified"]) if "email_verified" in row.keys() else False}
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
class SignUpInput(BaseModel): name: str = Field(min_length=1, max_length=30); email: str = Field(min_length=3, max_length=100); password: str = Field(min_length=4, max_length=100); role: str; partner_code: str = ""; email_code: str = ""
class LoginInput(BaseModel): email: str; password: str
class LinkInput(BaseModel): partner_code: str
class ActivityInput(BaseModel): activity_type: str; question: str; answer: str; correct_answer: str; is_correct: bool; encouragement: str; response_ms: Optional[int] = None
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
        verified = 0
        if mail_enabled():
            if conn.execute("SELECT 1 FROM users WHERE email = ?", (payload.email.strip().lower(),)).fetchone(): raise HTTPException(status_code=400, detail="이미 사용 중인 이메일입니다.")
            check_code(conn, payload.email, "signup", payload.email_code); verified = 1
        try: cursor = conn.execute("INSERT INTO users (name,email,password_hash,role,invite_code,linked_user_id,created_at,email_verified) VALUES (?,?,?,?,?,?,?,?)", (payload.name.strip(), payload.email.strip().lower(), password_hash(payload.password), payload.role, make_code(conn), linked_id, now(), verified))
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
# ---------- 이메일 인증 · 비밀번호 재설정 · 계정 설정 ----------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
def mail_enabled(): return MAIL_DEBUG or bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)
def normalize_email(email: str):
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email): raise HTTPException(status_code=400, detail="이메일 형식을 확인해 주세요.")
    return email
def code_hash(email: str, purpose: str, code: str): return hashlib.sha256(f"{email}:{purpose}:{code.strip()}".encode()).hexdigest()
def mask_email(email: str):
    local, _, domain = email.partition("@")
    return (local[:2] + "***" if len(local) > 2 else local[:1] + "***") + "@" + domain
def send_mail(to: str, subject: str, body: str):
    if MAIL_DEBUG:
        print(f"[MAIL_DEBUG] to={to} subject={subject}\n{body}", flush=True); return
    msg = EmailMessage(); msg["Subject"] = subject; msg["From"] = f"함께 돌봄 <{MAIL_FROM}>"; msg["To"] = to; msg.set_content(body)
    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as server: server.login(SMTP_USER, SMTP_PASSWORD); server.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server: server.starttls(); server.login(SMTP_USER, SMTP_PASSWORD); server.send_message(msg)
    except Exception as error:
        print(f"[MAIL_ERROR] {error!r}", flush=True)
        raise HTTPException(status_code=503, detail="메일을 보내지 못했어요. 잠시 후 다시 시도해 주세요.")
def issue_code(conn, email: str, purpose: str):
    stamp = int(time.time())
    last = conn.execute("SELECT created_at FROM email_codes WHERE email = ? AND purpose = ? ORDER BY id DESC LIMIT 1", (email, purpose)).fetchone()
    if last and stamp - last["created_at"] < CODE_RESEND_SECONDS: raise HTTPException(status_code=429, detail="1분 뒤에 다시 요청해 주세요.")
    if conn.execute("SELECT COUNT(*) FROM email_codes WHERE email = ? AND created_at > ?", (email, stamp - 3600)).fetchone()[0] >= CODE_MAX_PER_HOUR:
        raise HTTPException(status_code=429, detail="요청이 너무 많아요. 1시간 뒤에 다시 시도해 주세요.")
    code = f"{secrets.randbelow(10**6):06d}"
    conn.execute("INSERT INTO email_codes (email,purpose,code_hash,expires_at,created_at) VALUES (?,?,?,?,?)", (email, purpose, code_hash(email, purpose, code), stamp + CODE_TTL_SECONDS, stamp))
    return code
def check_code(conn, email: str, purpose: str, code: str):
    email = normalize_email(email)
    if not (code or "").strip(): raise HTTPException(status_code=400, detail="이메일로 받은 인증 코드를 입력해 주세요.")
    row = conn.execute("SELECT * FROM email_codes WHERE email = ? AND purpose = ? AND used_at IS NULL ORDER BY id DESC LIMIT 1", (email, purpose)).fetchone()
    if not row or row["expires_at"] < time.time(): raise HTTPException(status_code=400, detail="인증 코드가 만료되었어요. 코드를 다시 받아 주세요.")
    if row["attempts"] >= CODE_MAX_ATTEMPTS: raise HTTPException(status_code=400, detail="여러 번 틀렸어요. 코드를 다시 받아 주세요.")
    if not secrets.compare_digest(row["code_hash"], code_hash(email, purpose, code or "")):
        conn.execute("UPDATE email_codes SET attempts = attempts + 1 WHERE id = ?", (row["id"],)); conn.commit()
        raise HTTPException(status_code=400, detail="인증 코드가 맞지 않아요.")
    conn.execute("UPDATE email_codes SET used_at = ? WHERE id = ?", (int(time.time()), row["id"]))
CODE_MAIL = {
    "signup": ("[함께 돌봄] 가입 인증 코드", "함께 돌봄 가입 인증 코드는 {code} 입니다.\n10분 안에 앱에 입력해 주세요.\n\n본인이 요청하지 않았다면 이 메일은 무시해 주세요."),
    "verify": ("[함께 돌봄] 이메일 인증 코드", "함께 돌봄 이메일 인증 코드는 {code} 입니다.\n10분 안에 앱에 입력해 주세요."),
    "reset": ("[함께 돌봄] 비밀번호 재설정 코드", "함께 돌봄 비밀번호 재설정 코드는 {code} 입니다.\n10분 안에 앱에 입력해 주세요.\n\n본인이 요청하지 않았다면 이 메일은 무시해 주세요. 비밀번호는 바뀌지 않아요."),
}
def mail_code(email: str, purpose: str):
    with get_db() as conn: code = issue_code(conn, email, purpose)
    subject, body = CODE_MAIL[purpose]; send_mail(email, subject, body.format(code=code))
class EmailCodeInput(BaseModel): email: str; purpose: str
class PasswordResetInput(BaseModel): email: str; code: str; new_password: str = Field(min_length=4, max_length=100)
class FindEmailInput(BaseModel): name: str = Field(min_length=1, max_length=30); partner_email: str
class NameInput(BaseModel): name: str = Field(min_length=1, max_length=30)
class PasswordChangeInput(BaseModel): current_password: str; new_password: str = Field(min_length=4, max_length=100)
class CodeInput(BaseModel): code: str
class RoleInput(BaseModel): role: str
class DeleteAccountInput(BaseModel): password: str
@app.get("/api/auth/config")
def auth_config(): return {"email_verification": mail_enabled()}
@app.post("/api/auth/email-code")
def request_email_code(payload: EmailCodeInput):
    email = normalize_email(payload.email)
    if payload.purpose == "signup":
        if not mail_enabled(): raise HTTPException(status_code=400, detail="지금은 이메일 인증 없이 가입할 수 있어요.")
        with get_db() as conn:
            if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone(): raise HTTPException(status_code=400, detail="이미 사용 중인 이메일입니다.")
        mail_code(email, "signup"); return {"ok": True, "message": "인증 코드를 보냈어요. 메일함을 확인해 주세요."}
    if payload.purpose == "reset":
        if not mail_enabled(): raise HTTPException(status_code=400, detail="지금은 메일로 재설정할 수 없어요. 관리자에게 문의해 주세요.")
        with get_db() as conn: user = conn.execute("SELECT email_verified FROM users WHERE email = ?", (email,)).fetchone()
        # 가입 여부가 드러나지 않도록 결과와 상관없이 같은 안내를 돌려줍니다.
        if user and user["email_verified"]: mail_code(email, "reset")
        return {"ok": True, "message": "인증된 이메일로 가입한 계정이라면 재설정 코드를 보냈어요. 메일함을 확인해 주세요."}
    raise HTTPException(status_code=400, detail="요청 종류를 확인해 주세요.")
@app.post("/api/auth/password-reset")
def reset_password(payload: PasswordResetInput):
    email = normalize_email(payload.email)
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = ? AND email_verified = 1", (email,)).fetchone()
        if not user: raise HTTPException(status_code=400, detail="인증 코드가 만료되었어요. 코드를 다시 받아 주세요.")
        check_code(conn, email, "reset", payload.code)
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash(payload.new_password), user["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
    return {"ok": True}
@app.post("/api/auth/find-email")
def find_email(payload: FindEmailInput):
    partner_email = normalize_email(payload.partner_email)
    with get_db() as conn:
        partner = conn.execute("SELECT linked_user_id FROM users WHERE email = ?", (partner_email,)).fetchone()
        user = conn.execute("SELECT email, name FROM users WHERE id = ?", (partner["linked_user_id"],)).fetchone() if partner and partner["linked_user_id"] else None
    if not user or user["name"].strip() != payload.name.strip(): raise HTTPException(status_code=404, detail="일치하는 계정을 찾지 못했어요. 이름과 가족 이메일을 확인해 주세요.")
    return {"email_hint": mask_email(user["email"])}
@app.get("/api/account")
def account(user=Depends(current_user)):
    with get_db() as conn: partner = conn.execute("SELECT name, email, role FROM users WHERE id = ?", (user["linked_user_id"],)).fetchone() if user["linked_user_id"] else None
    return {"user": profile(user), "partner": dict(partner) if partner else None, "mail_enabled": mail_enabled()}
@app.patch("/api/account/name")
def change_name(payload: NameInput, user=Depends(current_user)):
    name = payload.name.strip()
    if not name: raise HTTPException(status_code=400, detail="이름을 입력해 주세요.")
    with get_db() as conn: conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, user["id"])); row = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    return {"user": profile(row)}
@app.post("/api/account/password")
def change_password(payload: PasswordChangeInput, user=Depends(current_user), authorization: Optional[str] = Header(default=None)):
    if not password_ok(payload.current_password, user["password_hash"]): raise HTTPException(status_code=400, detail="지금 비밀번호가 맞지 않아요.")
    token = (authorization or "").removeprefix("Bearer ")
    with get_db() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash(payload.new_password), user["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id = ? AND token != ?", (user["id"], token))
    return {"ok": True}
@app.post("/api/account/email-code")
def request_verify_code(user=Depends(current_user)):
    if not mail_enabled(): raise HTTPException(status_code=400, detail="아직 메일 발송이 설정되지 않았어요.")
    if user["email_verified"]: raise HTTPException(status_code=400, detail="이미 인증된 이메일이에요.")
    mail_code(user["email"], "verify"); return {"ok": True, "message": "인증 코드를 보냈어요. 메일함을 확인해 주세요."}
@app.post("/api/account/verify-email")
def verify_email(payload: CodeInput, user=Depends(current_user)):
    with get_db() as conn:
        check_code(conn, user["email"], "verify", payload.code)
        conn.execute("UPDATE users SET email_verified = 1 WHERE id = ?", (user["id"],)); row = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    return {"user": profile(row)}
@app.post("/api/account/unlink")
def unlink(user=Depends(current_user)):
    if not user["linked_user_id"]: raise HTTPException(status_code=400, detail="연결된 가족이 없어요.")
    with get_db() as conn:
        conn.execute("UPDATE users SET linked_user_id = NULL WHERE id IN (?, ?)", (user["id"], user["linked_user_id"])); row = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    return {"user": profile(row)}
@app.post("/api/account/role")
def change_role(payload: RoleInput, user=Depends(current_user)):
    if payload.role not in ("guardian", "senior"): raise HTTPException(status_code=400, detail="모드를 다시 선택해 주세요.")
    if user["linked_user_id"]: raise HTTPException(status_code=400, detail="가족 연결을 해제한 뒤 모드를 바꿀 수 있어요.")
    with get_db() as conn: conn.execute("UPDATE users SET role = ? WHERE id = ?", (payload.role, user["id"])); row = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    return {"user": profile(row)}
@app.post("/api/account/delete")
def delete_account(payload: DeleteAccountInput, user=Depends(current_user)):
    if not password_ok(payload.password, user["password_hash"]): raise HTTPException(status_code=400, detail="비밀번호가 맞지 않아요.")
    uid = user["id"]
    with get_db() as conn:
        post_ids = [row["id"] for row in conn.execute("SELECT id FROM timeline_posts WHERE author_id = ?", (uid,)).fetchall()]
        marks = ",".join("?" for _ in post_ids) or "NULL"
        files = [row["file_name"] for row in conn.execute(f"SELECT file_name FROM post_media WHERE user_id = ? OR post_id IN ({marks})", (uid, *post_ids)).fetchall()]
        conn.execute(f"DELETE FROM post_media WHERE user_id = ? OR post_id IN ({marks})", (uid, *post_ids))
        conn.execute(f"DELETE FROM post_likes WHERE user_id = ? OR post_id IN ({marks})", (uid, *post_ids))
        conn.execute(f"DELETE FROM post_comments WHERE user_id = ? OR post_id IN ({marks})", (uid, *post_ids))
        conn.execute("DELETE FROM timeline_posts WHERE author_id = ?", (uid,))
        for table in ("sessions", "activities", "quests", "app_state", "user_app_state", "quest_progress", "quest_completions", "mood_checkins", "meetup_interests"):
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM email_codes WHERE email = ?", (user["email"],))
        conn.execute("UPDATE users SET linked_user_id = NULL WHERE linked_user_id = ?", (uid,))
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    for name in files:
        try: os.remove(os.path.join(UPLOAD_DIR, os.path.basename(name)))
        except OSError: pass
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
        # 풀이 시간(밀리초)은 0~10분 사이만 저장합니다. 리포트 계산에서는 2분이 넘는 값은 빼요.
        response_ms = payload.response_ms if payload.response_ms is not None and 0 < payload.response_ms <= 600000 else None
        cursor = conn.execute("INSERT INTO activities (activity_type,question,answer,correct_answer,is_correct,encouragement,completed_at,user_id,response_ms) VALUES (?,?,?,?,?,?,?,?,?)", (payload.activity_type,payload.question,payload.answer,payload.correct_answer,int(payload.is_correct),payload.encouragement,now(),user["id"],response_ms)); row = conn.execute("SELECT * FROM activities WHERE id = ?", (cursor.lastrowid,)).fetchone()
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
        today_count = conn.execute("SELECT COUNT(*) FROM quest_completions WHERE user_id = ? AND kdate = ?", (user["id"], today_korea())).fetchone()[0]
        if today_count >= QUEST_DAILY_LIMIT: raise HTTPException(status_code=429, detail=f"오늘은 퀘스트를 {QUEST_DAILY_LIMIT}개 모두 했어요. 내일 또 만나요!")
        conn.execute("INSERT INTO quest_completions (user_id,quest_key,round,completed_at,kdate) VALUES (?,?,?,?,?)", (user["id"], payload.quest_key, quest_round(conn, user["id"]), timestamp, today_korea()))
        conn.execute("""INSERT INTO quest_progress (user_id,quest_key,title,role,completed,reflection,result_summary,completed_at,is_shared,media_kind,media_count) VALUES (?,?,?,?,1,?,?,?,?,?,?) ON CONFLICT(user_id,quest_key) DO UPDATE SET completed=1, reflection=excluded.reflection, result_summary=excluded.result_summary, completed_at=excluded.completed_at, is_shared=excluded.is_shared, media_kind=excluded.media_kind, media_count=excluded.media_count""", (user["id"],payload.quest_key,payload.title,user["role"],payload.reflection.strip(),payload.result_summary.strip(),timestamp,int(payload.is_shared),payload.media_kind,max(0,payload.media_count)))
        row = conn.execute("SELECT * FROM quest_progress WHERE user_id = ? AND quest_key = ?", (user["id"], payload.quest_key)).fetchone()
        post_id = None
        if payload.is_shared: post_id = conn.execute("INSERT INTO timeline_posts (author_id,quest_key,title,body,result_summary,created_at) VALUES (?,?,?,?,?,?)", (user["id"], payload.quest_key, payload.title, payload.reflection.strip(), payload.result_summary.strip(), timestamp)).lastrowid
    return progress_dict(row) | {"post_id": post_id, "today_count": today_count + 1}
def quest_round(conn, user_id: int):
    row = conn.execute("SELECT state_value FROM user_app_state WHERE user_id = ? AND state_key = 'quest_round'", (user_id,)).fetchone()
    return int(row["state_value"]) if row else 1
def quest_status(conn, user_id: int):
    round_no = quest_round(conn, user_id)
    keys = [row["quest_key"] for row in conn.execute("SELECT DISTINCT quest_key FROM quest_completions WHERE user_id = ? AND round = ?", (user_id, round_no)).fetchall()]
    today = conn.execute("SELECT COUNT(*) FROM quest_completions WHERE user_id = ? AND kdate = ?", (user_id, today_korea())).fetchone()[0]
    return {"round": round_no, "completed_keys": keys, "today_count": today, "daily_limit": QUEST_DAILY_LIMIT}
@app.get("/api/quest-status")
def get_quest_status(user=Depends(current_user)):
    with get_db() as conn: return quest_status(conn, user["id"])
@app.post("/api/quest-round/next")
def next_quest_round(user=Depends(current_user)):
    # 모든 단계를 마친 뒤 '처음부터 다시 도전하기'
    with get_db() as conn:
        next_round = quest_round(conn, user["id"]) + 1
        conn.execute("INSERT OR REPLACE INTO user_app_state (user_id,state_key,state_value,updated_at) VALUES (?,'quest_round',?,?)", (user["id"], str(next_round), now()))
        return quest_status(conn, user["id"])
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
    # 사진과 녹음 파일을 실제 내용으로 구분합니다. (확장자나 요청 헤더는 믿지 않음)
    if data[:4] == b"\x1a\x45\xdf\xa3": return "audio/webm", ".webm"
    if data[:4] == b"OggS": return "audio/ogg", ".ogg"
    if data[4:8] == b"ftyp": return "audio/mp4", ".m4a"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE": return "audio/wav", ".wav"
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0 and data[:3] != b"\xff\xd8\xff"): return "audio/mpeg", ".mp3"
    if data.startswith(b"\xff\xd8\xff"): return "image/jpeg", ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"): return "image/png", ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP": return "image/webp", ".webp"
    return None, None
@app.post("/api/timeline/{post_id}/media")
async def upload_media(post_id: int, request: Request, user=Depends(current_user)):
    # 사진 파일 자체를 요청 본문으로 받습니다. (추가 패키지 없이 동작)
    with get_db() as conn:
        post = allowed_post(post_id, user, conn)
        if post["author_id"] != user["id"]: raise HTTPException(status_code=403, detail="내 게시물에만 사진이나 녹음을 올릴 수 있습니다.")
    data = b""
    async for chunk in request.stream():
        data += chunk
        if len(data) > AUDIO_MAX_BYTES: raise HTTPException(status_code=400, detail="파일이 너무 커요. 녹음은 10MB, 사진은 5MB까지 올릴 수 있어요.")
    content_type, ext = image_type(data)
    if not content_type: raise HTTPException(status_code=400, detail="jpg, png, webp 사진이나 녹음 파일만 올릴 수 있어요.")
    is_audio = content_type.startswith("audio/")
    if not is_audio and len(data) > MEDIA_MAX_BYTES: raise HTTPException(status_code=400, detail="사진은 한 장에 5MB까지 올릴 수 있어요.")
    if not is_audio:
        with get_db() as conn:
            if conn.execute("SELECT COUNT(*) FROM post_media WHERE post_id = ? AND content_type LIKE 'image/%'", (post_id,)).fetchone()[0] >= MEDIA_MAX_PER_POST:
                raise HTTPException(status_code=400, detail=f"사진은 게시물당 {MEDIA_MAX_PER_POST}장까지 올릴 수 있어요.")
    if is_audio:
        with get_db() as conn:
            if conn.execute("SELECT COUNT(*) FROM post_media WHERE post_id = ? AND content_type LIKE 'audio/%'", (post_id,)).fetchone()[0] >= 1:
                raise HTTPException(status_code=400, detail="녹음은 게시물당 1개까지 올릴 수 있어요.")
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
# ---------- 관계 리포트 (활동 기반 5항목) ----------
# 배점과 기준은 여기서만 바꾸면 됩니다.
REPORT_POINTS = {
    "contact_quest": 40, "share": 30, "comment": 15, "like": 5,          # 연락
    "quest_done": 50, "quest_share": 25, "quest_care": 25,                 # 오늘의 퀘스트
    "mood": 50, "mind_quest": 50,                                          # 마음
    "brain_solve": 4, "brain_correct": 12, "brain_speed": 20,              # 두뇌 (문제당 풀이·정답, 속도)
}
REPORT_DAILY_QUESTIONS = 5          # 오늘의 두뇌 체조 문제 수
REPORT_CARE_TEXT_LEN = 30           # '정성 기록'으로 보는 글 길이
REPORT_TIME_CAP_MS = 120000         # 2분이 넘는 풀이 시간은 계산에서 제외
REPORT_SPEED_MIN_DAYS = 7           # 속도 점수에 필요한 이전 기록 일수
REPORT_CHANGE = {"recent_days": 14, "base_days": 30, "min_answers": 10, "acc_drop": 20, "time_ratio": 1.5}
REPORT_LABELS = {"contact": "오늘의 연락", "move": "신체 움직임", "quest": "오늘의 퀘스트", "mind": "마음", "brain": "두뇌 활동"}
# 퀘스트 분류 (앱의 questData.ts와 같게 유지)
QUEST_CATEGORIES = {
    "senior-flowers": "move", "senior-selfie": "mind", "senior-diary": "brain", "senior-draw": "mind", "senior-thanks": "mind",
    "senior-song": "mind", "senior-smile": "mind", "senior-season-walk": "move", "senior-praise": "contact", "senior-season-story": "brain",
    "senior-old-song": "brain", "senior-gift": "brain", "senior-favorite-place": "move", "senior-ask-happy": "contact", "senior-hometown": "brain",
    "senior-school-friend": "brain", "senior-holiday": "brain", "senior-recipe": "contact", "senior-first-pay": "brain", "senior-child": "brain",
    "senior-trip": "brain", "senior-folk-tale": "contact", "senior-happiest-day": "mind", "senior-motto": "mind", "senior-self-cheer": "mind",
    "senior-wish": "mind", "guardian-hello": "contact", "guardian-calendar": "mind", "guardian-letter": "contact", "guardian-story": "contact",
}
MOOD_TEXT = {"good": "좋아요", "okay": "보통", "hard": "힘들어요"}
COG_TYPE_LABEL = {"날짜": "날짜·시간", "계산": "계산", "기억": "기억", "언어": "말·속담", "상식": "생활 상식"}
def _median(values):
    values = sorted(values)
    if not values: return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
def _marks(ids): return ",".join("?" for _ in ids)
def _day_shift(day: str, delta: int): return (datetime.fromisoformat(day) + timedelta(days=delta)).date().isoformat()
def _day_answers(conn, senior_id: int, day: str):
    # 그날 처음 푼 5문제 (한 번 더 풀기는 점수에 넣지 않음)
    return conn.execute("SELECT activity_type, is_correct, response_ms FROM activities WHERE user_id = ? AND substr(completed_at,1,10) = ? ORDER BY id LIMIT ?", (senior_id, day, REPORT_DAILY_QUESTIONS)).fetchall()
def _valid_ms(rows): return [row["response_ms"] for row in rows if row["response_ms"] and row["response_ms"] <= REPORT_TIME_CAP_MS]
def _speed_baseline(conn, senior_id: int, day: str):
    # 그날 이전 30일 동안의 '하루 풀이 시간 중간값'들의 중간값 (본인 평소)
    medians = []
    for back in range(1, 31):
        values = _valid_ms(_day_answers(conn, senior_id, _day_shift(day, -back)))
        if values: medians.append(_median(values))
    return _median(medians) if len(medians) >= REPORT_SPEED_MIN_DAYS else None
def report_day(conn, senior_id: int, partner_id, day: str, with_speed: bool = True):
    P = REPORT_POINTS; members = [senior_id] + ([partner_id] if partner_id else [])
    done = conn.execute(f"SELECT c.user_id, c.quest_key, (SELECT title FROM quest_progress q WHERE q.user_id = c.user_id AND q.quest_key = c.quest_key) AS title FROM quest_completions c WHERE c.user_id IN ({_marks(members)}) AND c.kdate = ?", (*members, day)).fetchall()
    mine = [row for row in done if row["user_id"] == senior_id]
    of = lambda rows, cat: [row for row in rows if QUEST_CATEGORIES.get(row["quest_key"]) == cat]
    titles = lambda rows: ", ".join(dict.fromkeys(row["title"] or row["quest_key"] for row in rows))
    count = lambda sql, *args: conn.execute(sql, args).fetchone()[0]
    shares = count(f"SELECT COUNT(*) FROM timeline_posts WHERE author_id IN ({_marks(members)}) AND deleted_at IS NULL AND substr(created_at,1,10) = ?", *members, day)
    comments = count(f"SELECT COUNT(*) FROM post_comments WHERE user_id IN ({_marks(members)}) AND deleted_at IS NULL AND substr(created_at,1,10) = ?", *members, day)
    likes = count(f"SELECT COUNT(*) FROM post_likes WHERE user_id IN ({_marks(members)}) AND substr(created_at,1,10) = ?", *members, day)
    items = {}
    # 1. 오늘의 연락 (시니어·보호자 둘 다)
    contact_q = of(done, "contact")
    score = min(100, (P["contact_quest"] if contact_q else 0) + P["share"] * shares + P["comment"] * comments + P["like"] * likes)
    parts = ([f"연락 퀘스트({titles(contact_q)})"] if contact_q else []) + ([f"가족 소식 공유 {shares}건"] if shares else []) + ([f"댓글 {comments}개"] if comments else []) + ([f"좋아요 {likes}개"] if likes else [])
    items["contact"] = {"score": score, "detail": " · ".join(parts) if parts else "오늘은 가족과 주고받은 소식이 없어요"}
    # 2. 신체 움직임 (기록이 없으면 종합에서 제외)
    move_q = of(mine, "move")
    items["move"] = {"score": 100 if move_q else None, "detail": f"{titles(move_q)} 완료" if move_q else "오늘은 움직임 기록이 없어요"}
    # 3. 오늘의 퀘스트: 완료 + 가족 공유 + 정성 기록
    my_posts = conn.execute("SELECT p.id, p.body, (SELECT COUNT(*) FROM post_media m WHERE m.post_id = p.id) AS media FROM timeline_posts p WHERE p.author_id = ? AND p.deleted_at IS NULL AND substr(p.created_at,1,10) = ?", (senior_id, day)).fetchall()
    progress_rows = conn.execute("SELECT reflection, media_count FROM quest_progress WHERE user_id = ? AND substr(completed_at,1,10) = ?", (senior_id, day)).fetchall()
    cared = any(row["media"] or len((row["body"] or "").strip()) >= REPORT_CARE_TEXT_LEN for row in my_posts) or any(row["media_count"] or len((row["reflection"] or "").strip()) >= REPORT_CARE_TEXT_LEN for row in progress_rows)
    score = (P["quest_done"] if mine else 0) + (P["quest_share"] if mine and my_posts else 0) + (P["quest_care"] if mine and cared else 0)
    parts = ([f"{titles(mine)} 완료"] if mine else []) + (["가족과 공유"] if mine and my_posts else []) + (["정성 기록(녹음·사진·글)"] if mine and cared else [])
    items["quest"] = {"score": score, "detail": " · ".join(parts) if parts else "오늘은 아직 퀘스트를 하지 않았어요", "count": len(mine)}
    # 4. 마음: 기분 기록 + 마음 퀘스트
    mood = conn.execute("SELECT mood FROM mood_checkins WHERE user_id = ? AND checkin_date = ?", (senior_id, day)).fetchone()
    mind_q = of(mine, "mind")
    score = (P["mood"] if mood else 0) + (P["mind_quest"] if mind_q else 0)
    parts = ([f"오늘 기분: {MOOD_TEXT.get(mood['mood'], mood['mood'])}"] if mood else []) + ([f"마음 퀘스트({titles(mind_q)})"] if mind_q else [])
    items["mind"] = {"score": score, "detail": " · ".join(parts) if parts else "오늘은 아직 기분을 남기지 않았어요", "mood": mood["mood"] if mood else None}
    # 5. 두뇌: 풀이 + 정답 + 속도(본인 평소 대비)
    answers = _day_answers(conn, senior_id, day)
    solved, correct = len(answers), sum(1 for row in answers if row["is_correct"])
    today_ms = _median(_valid_ms(answers)); base_ms = _speed_baseline(conn, senior_id, day) if with_speed and today_ms else None
    speed = None
    if today_ms and base_ms: speed = P["brain_speed"] if today_ms <= base_ms else round(P["brain_speed"] * max(0.0, 1 - (today_ms / base_ms - 1)))
    raw = P["brain_solve"] * solved + P["brain_correct"] * correct
    score = (raw + speed) if speed is not None else round(raw / ((P["brain_solve"] + P["brain_correct"]) * REPORT_DAILY_QUESTIONS) * 100)
    speed_text = "" if speed is None else (" · 평소보다 빨리 풀었어요" if today_ms <= base_ms * 0.9 else " · 평소와 비슷한 속도예요" if today_ms <= base_ms * 1.2 else " · 평소보다 천천히 풀었어요")
    items["brain"] = {"score": score if solved else 0, "detail": f"{solved}문제 중 {correct}문제를 맞혔어요{speed_text}" if solved else "오늘의 5문제를 아직 풀지 않았어요",
                      "solved": solved, "correct": correct, "median_seconds": round(today_ms / 1000, 1) if today_ms else None, "base_seconds": round(base_ms / 1000, 1) if base_ms else None, "speed_score": speed}
    counted = [item["score"] for item in items.values() if item["score"] is not None]
    return {"date": day, "items": items, "overall": round(sum(counted) / len(counted)) if counted else 0}
def _brain_window(conn, senior_id: int, start: str, end: str):
    rows = conn.execute("SELECT activity_type, is_correct, response_ms FROM activities WHERE user_id = ? AND substr(completed_at,1,10) BETWEEN ? AND ?", (senior_id, start, end)).fetchall()
    def summary(subset):
        ms = _valid_ms(subset)
        return {"answers": len(subset), "accuracy": round(sum(1 for r in subset if r["is_correct"]) / len(subset) * 100) if subset else None, "median_seconds": round(_median(ms) / 1000, 1) if ms else None}
    types = sorted({row["activity_type"] for row in rows})
    return summary(rows), {t: summary([r for r in rows if r["activity_type"] == t]) for t in types}
def _brain_change(conn, senior_id: int, today: str):
    C = REPORT_CHANGE
    recent_start = _day_shift(today, -(C["recent_days"] - 1)); base_end = _day_shift(recent_start, -1); base_start = _day_shift(base_end, -(C["base_days"] - 1))
    recent, recent_types = _brain_window(conn, senior_id, recent_start, today)
    base, base_types = _brain_window(conn, senior_id, base_start, base_end)
    if recent["answers"] < C["min_answers"] or base["answers"] < C["min_answers"]:
        return {"status": "insufficient", "message": "기록이 쌓이면(약 6주) 평소와 비교한 변화를 알려 드려요.", "recent": recent, "base": base}
    found = []
    subject = lambda name: "두뇌 체조" if name == "전체" else f"{COG_TYPE_LABEL.get(name, name)} 문제"
    for name, now_s, base_s in [("전체", recent, base)] + [(t, recent_types[t], base_types[t]) for t in recent_types if t in base_types and recent_types[t]["answers"] >= 3 and base_types[t]["answers"] >= 3]:
        if now_s["accuracy"] is not None and base_s["accuracy"] is not None and base_s["accuracy"] - now_s["accuracy"] >= C["acc_drop"]:
            found.append(f"최근 2주 {subject(name)} 정답률이 평소보다 낮아졌어요 (평소 {base_s['accuracy']}% → 최근 {now_s['accuracy']}%)")
        if now_s["median_seconds"] and base_s["median_seconds"] and now_s["median_seconds"] >= base_s["median_seconds"] * C["time_ratio"]:
            found.append(f"최근 2주 {subject(name)} 풀이 시간이 평소보다 길어졌어요 (평소 {base_s['median_seconds']}초 → 최근 {now_s['median_seconds']}초)")
    if found:
        return {"status": "changed", "message": found[0] + ". 일시적일 수 있지만, 걱정되시면 가까운 치매안심센터나 전문가와 상담해 보세요.", "details": found, "recent": recent, "base": base}
    return {"status": "stable", "message": "최근 2주 정답률과 풀이 시간이 평소와 비슷해요.", "recent": recent, "base": base}
SENIOR_TIPS = {"contact": "오늘 한 일을 가족과 나눠 볼까요?", "quest": "오늘의 퀘스트를 하나 해 볼까요?", "mind": "오늘 기분을 남겨 볼까요?", "brain": "오늘의 두뇌 체조 5문제를 풀어 볼까요?"}
GUARDIAN_TIPS = {
    "contact": ("어르신 소식에 반응해 주세요", "가족 소식에 댓글이나 좋아요를 남기면 연락 점수가 올라가고, 어르신께 큰 힘이 돼요."),
    "quest": ("퀘스트를 함께 응원해 주세요", "하루 한 개 퀘스트를 꾸준히 하실 수 있도록 안부 전화로 응원해 보세요."),
    "mind": ("마음을 살펴 주세요", "기분 기록이 적거나 힘든 날이 있었어요. 짧은 통화로 마음을 나눠 보세요."),
    "brain": ("두뇌 체조를 권해 주세요", "하루 5문제를 꾸준히 풀면 변화를 더 정확하게 살펴볼 수 있어요."),
}
@app.get("/api/report/daily")
def daily_report(user=Depends(current_user)):
    senior_id = activity_owner(user)
    if user["role"] == "guardian" and not user["linked_user_id"]: return {"linked": False}
    with get_db() as conn:
        senior = conn.execute("SELECT * FROM users WHERE id = ?", (senior_id,)).fetchone()
        partner_id = senior["linked_user_id"]; today = today_korea(); signup = (senior["created_at"] or today)[:10]
        days = [report_day(conn, senior_id, partner_id, _day_shift(today, -back), with_speed=True) for back in range(13, -1, -1)]
        this_week, last_week = days[7:], days[:7]
        window = [d for d in this_week if d["date"] >= signup] or [this_week[-1]]
        signal_score = round(sum(d["overall"] for d in window) / len(window))
        def week_avg(group, key):
            values = [d["items"][key]["score"] for d in group if d["items"][key]["score"] is not None and d["date"] >= signup]
            return round(sum(values) / len(values)) if values else None
        compare = {key: {"this_week": week_avg(this_week, key), "last_week": week_avg(last_week, key)} for key in REPORT_LABELS}
        today_report = days[-1]
        lowest_today = sorted((item["score"], key) for key, item in today_report["items"].items() if item["score"] is not None and key in SENIOR_TIPS)
        senior_tip = SENIOR_TIPS[lowest_today[0][1]] if lowest_today and lowest_today[0][0] < 70 else "오늘도 정말 잘하셨어요!"
        brain_change = _brain_change(conn, senior_id, today)
        recs = []
        if brain_change["status"] == "changed": recs.append({"title": "두뇌 체조 변화를 살펴봐 주세요", "reason": brain_change["message"]})
        for key in sorted(GUARDIAN_TIPS, key=lambda k: compare[k]["this_week"] if compare[k]["this_week"] is not None else 0):
            avg = compare[key]["this_week"]
            if avg is None or avg >= 70 or len(recs) >= 3: continue
            last = compare[key]["last_week"]
            trend = "" if last is None else f" (지난주 {last}점)" if last == avg else f" (지난주 {last}점보다 {'낮아요' if avg < last else '높아요'})"
            recs.append({"title": GUARDIAN_TIPS[key][0], "reason": f"이번 주 {REPORT_LABELS[key]} 평균 {avg}점{trend}. {GUARDIAN_TIPS[key][1]}"})
        if not recs: recs.append({"title": "지금처럼 함께해 주세요", "reason": "이번 주 활동이 고르게 이어지고 있어요. 꾸준한 관심이 가장 큰 돌봄이에요."})
        consistency = {"quest_days": sum(1 for d in window if d["items"]["quest"]["score"]), "brain_days": sum(1 for d in window if d["items"]["brain"]["solved"]),
                       "mood_days": sum(1 for d in window if d["items"]["mind"]["mood"]), "days": len(window)}
        brain_7 = _brain_window(conn, senior_id, _day_shift(today, -6), today); brain_30 = _brain_window(conn, senior_id, _day_shift(today, -29), today)
    return {
        "linked": True, "senior_name": senior["name"], "date": today,
        "items": [{"key": key, "label": REPORT_LABELS[key], **today_report["items"][key]} for key in REPORT_LABELS],
        "overall": today_report["overall"], "signal_score": signal_score, "signal_days": len(window),
        "days": [{"date": d["date"], "overall": d["overall"], "scores": {k: v["score"] for k, v in d["items"].items()}} for d in this_week],
        "compare": compare, "consistency": consistency, "senior_tip": senior_tip, "recommendations": recs[:3],
        "brain": {"change": brain_change, "week": {"all": brain_7[0], "by_type": brain_7[1]}, "month": {"all": brain_30[0], "by_type": brain_30[1]}},
    }
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
