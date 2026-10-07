import hashlib
import hmac
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REPO_ROOT = Path(__file__).resolve().parent.parent
COOKIE_NAME = "reader_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
DOCUMENT_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    if value.lower() not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name} 必须为 true 或 false")
    return value.lower() in {"true", "1"}


def normalize_origin(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            return None
        port = parsed.port
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            host += f":{port}"
        return f"{parsed.scheme}://{host}"
    except ValueError:
        return None


@dataclass(frozen=True)
class Settings:
    password: str = field(default="", repr=False)
    data_dir: Path = Path("data")
    static_dir: Path = REPO_ROOT / "frontend" / "dist"
    cookie_secure: bool = False
    trusted_origins: tuple[str, ...] = ()
    max_upload_bytes: int = 5 * 1024 * 1024
    session_ttl_seconds: int = 30 * 24 * 60 * 60
    max_login_failures: int = 8
    login_window_seconds: int = 5 * 60

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            password=os.environ.get("READER_PASSWORD", ""),
            data_dir=Path(os.environ.get("READER_DATA_DIR", "data")),
            static_dir=Path(os.environ.get("READER_STATIC_DIR", str(REPO_ROOT / "frontend" / "dist"))),
            cookie_secure=env_bool("READER_COOKIE_SECURE", False),
            trusted_origins=tuple(
                value.strip()
                for value in os.environ.get("READER_TRUSTED_ORIGINS", "").split(",")
                if value.strip()
            ),
            max_upload_bytes=int(os.environ.get("READER_MAX_UPLOAD_BYTES", str(5 * 1024 * 1024))),
        )

    def validate(self) -> None:
        if not self.password.strip():
            raise RuntimeError("必须配置 READER_PASSWORD 后才能启动墨读")
        if len(self.password) > 1024:
            raise RuntimeError("READER_PASSWORD 长度不能超过 1024 个字符")
        try:
            self.password.encode("utf-8")
        except UnicodeEncodeError:
            raise RuntimeError("READER_PASSWORD 必须包含有效的 Unicode 字符") from None
        if self.max_upload_bytes <= 0:
            raise RuntimeError("READER_MAX_UPLOAD_BYTES 必须大于 0")
        if self.session_ttl_seconds <= 0 or self.max_login_failures <= 0 or self.login_window_seconds <= 0:
            raise RuntimeError("会话期限和登录限流设置必须大于 0")
        if any(normalize_origin(origin) is None for origin in self.trusted_origins):
            raise RuntimeError("READER_TRUSTED_ORIGINS 只能包含完整的 HTTP/HTTPS 源地址")


class RequestSizeMiddleware:
    """Bound multipart bodies before Starlette can spool arbitrarily large uploads."""

    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if scope["type"] != "http" or method in SAFE_METHODS or not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return
        limit = self.settings.max_upload_bytes + 65536 if path == "/api/documents" and method == "POST" else 65536
        content_length = Headers(scope=scope).get("content-length")
        if content_length:
            try:
                if int(content_length) > limit or int(content_length) < 0:
                    await JSONResponse({"detail": "请求内容超出大小限制"}, status_code=413)(scope, receive, send)
                    return
            except ValueError:
                await JSONResponse({"detail": "Content-Length 无效"}, status_code=400)(scope, receive, send)
                return
        chunks: list[bytes] = []
        length = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            length += len(chunk)
            if length > limit:
                await JSONResponse({"detail": "请求内容超出大小限制"}, status_code=413)(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class SecurityMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.trusted_origins = {normalize_origin(value) for value in settings.trusted_origins}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)

        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "no-referrer"
                headers["X-Frame-Options"] = "DENY"
                headers["Content-Security-Policy"] = (
                    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data: https:; font-src 'self'; connect-src 'self'; "
                    "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
                )
                if request.url.path.startswith("/api/"):
                    headers["Cache-Control"] = "no-store"
            await send(message)

        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            own_origin = normalize_origin(f"{request.url.scheme}://{request.url.netloc}")
            if (
                request.headers.get("sec-fetch-site", "").lower() == "cross-site"
                or (origin is not None and normalize_origin(origin) not in {own_origin, *self.trusted_origins})
                or (origin is not None and normalize_origin(origin) is None)
            ):
                await JSONResponse({"detail": "拒绝跨站请求"}, status_code=403)(scope, receive, secure_send)
                return
        await self.app(scope, receive, secure_send)


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.data_dir.resolve()
        self.documents_dir = self.root / "documents"
        self.database = self.root / "reader.sqlite3"
        self.salt = b""
        self.password_hash = b""
        self.csrf_secret = b""

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.database, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        self.settings.validate()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.documents_dir.mkdir(exist_ok=True, mode=0o700)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    title TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_opened_at TEXT,
                    UNIQUE(filename, sha256)
                );
                CREATE TABLE IF NOT EXISTS progress (
                    document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
                    anchor TEXT,
                    offset REAL NOT NULL DEFAULT 0,
                    percent REAL NOT NULL DEFAULT 0,
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS progress_writes (
                    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    client_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    PRIMARY KEY(document_id, client_id)
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_expiration ON sessions(expires_at);
                CREATE TABLE IF NOT EXISTS preferences (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    font_size INTEGER NOT NULL DEFAULT 18,
                    theme TEXT NOT NULL DEFAULT 'system'
                );
                INSERT OR IGNORE INTO preferences(id) VALUES (1);
            """)
            conn.execute("BEGIN IMMEDIATE")
            existing = dict(conn.execute("SELECT key, value FROM metadata").fetchall())
            self.salt = bytes.fromhex(existing.get("password_salt", secrets.token_hex(16)))
            self.password_hash = self.hash_password(self.settings.password)
            if not hmac.compare_digest(existing.get("password_hash", ""), self.password_hash.hex()):
                conn.execute("DELETE FROM sessions")
            self.csrf_secret = bytes.fromhex(existing.get("csrf_secret", secrets.token_hex(32)))
            for key, value in {
                "password_salt": self.salt.hex(),
                "password_hash": self.password_hash.hex(),
                "csrf_secret": self.csrf_secret.hex(),
            }.items():
                conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)", (key, value))
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (time.time(),))
            # Finish a deletion interrupted between the rename and database commit.
            known = {row["id"] for row in conn.execute("SELECT id FROM documents")}
            for path in self.documents_dir.glob("*.trash"):
                document_id = path.stem
                if DOCUMENT_ID.fullmatch(document_id) is None or not path.is_file():
                    continue
                if document_id in known and not self.file_path(document_id).exists():
                    path.replace(self.file_path(document_id))
                else:
                    path.unlink()
            # Crash before upload commit can leave an orphan; never expose it.
            for path in self.documents_dir.iterdir():
                if path.is_file() and DOCUMENT_ID.fullmatch(path.stem) and (path.suffix == ".tmp" or (path.suffix == ".md" and path.stem not in known)):
                    path.unlink()
        try:
            self.database.chmod(0o600)
        except OSError:
            pass

    def hash_password(self, value: str) -> bytes:
        return hashlib.scrypt(value.encode("utf-8"), salt=self.salt, n=16384, r=8, p=1, dklen=32)

    def file_path(self, document_id: str) -> Path:
        if DOCUMENT_ID.fullmatch(document_id) is None:
            raise HTTPException(404, "文件不存在")
        return self.documents_dir / f"{document_id}.md"

    def csrf_token(self, raw_session_token: str) -> str:
        return hmac.new(self.csrf_secret, raw_session_token.encode("ascii"), hashlib.sha256).hexdigest()


class LoginLimiter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.failures: dict[str, deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, address: str) -> None:
        now = time.monotonic()
        with self.lock:
            for key in list(self.failures):
                queue = self.failures[key]
                while queue and queue[0] <= now - self.settings.login_window_seconds:
                    queue.popleft()
                if not queue:
                    del self.failures[key]
            queue = self.failures.get(address, ())
            if len(queue) >= self.settings.max_login_failures:
                retry = math.ceil(queue[0] + self.settings.login_window_seconds - now)
                raise HTTPException(429, "登录尝试过多，请稍后重试", headers={"Retry-After": str(max(1, retry))})
            # Reserve this attempt before the expensive hash, including concurrent requests.
            self.failures[address].append(time.monotonic())

    def clear(self, address: str) -> None:
        with self.lock:
            self.failures.pop(address, None)


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    password: str = Field(min_length=1, max_length=1024)

    @field_validator("password")
    @classmethod
    def valid_unicode(cls, value: str) -> str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("密码包含无效字符") from None
        return value


class PreferencesInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    font_size: int = Field(ge=16, le=24)
    theme: Literal["light", "dark", "system"]


class ProgressInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    anchor: str | None = Field(default=None, max_length=160)
    offset: float = Field(ge=0, le=1)
    percent: float = Field(ge=0, le=100)
    client_id: str | None = Field(default=None, min_length=16, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    sequence: int | None = Field(default=None, ge=0, le=9007199254740991)

    @model_validator(mode="after")
    def ordering_pair(self):
        if (self.client_id is None) != (self.sequence is None):
            raise ValueError("client_id 和 sequence 必须同时提供")
        return self

    @field_validator("anchor")
    @classmethod
    def safe_anchor(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"(?:block|heading)-\d+", value) is None:
            raise ValueError("anchor 必须是阅读内容块的标识")
        return value


@dataclass(frozen=True)
class Session:
    token: str
    token_hash: str


def title_from_markdown(content: str, filename: str) -> str:
    fence: str | None = None
    fence_length = 0
    previous = ""
    for line in content.lstrip("\ufeff").splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if marker and marker[1][0] == fence and len(marker[1]) >= fence_length and not marker[2].strip():
                fence = None
            continue
        if marker:
            fence, fence_length = marker[1][0], len(marker[1])
            previous = ""
            continue
        if line.startswith("    ") or line.startswith("\t"):
            previous = ""
            continue
        heading = re.match(r"^ {0,3}#{1,6}[ \t]+(.+)$", line)
        title = None
        if heading:
            title = re.sub(r"[ \t]+#+[ \t]*$", "", heading[1]).strip()
        elif previous and re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*", line):
            title = previous.strip()
        if title:
            title = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", title)
            title = re.sub(r"[`*_]", "", title)
            return title[:200]
        previous = line if line.strip() else ""
    return Path(filename).stem[:200]


DOCUMENT_SELECT = """
    SELECT d.*, p.anchor, COALESCE(p.offset,0) AS offset,
           COALESCE(p.percent,0) AS percent, p.updated_at AS progress_updated_at
    FROM documents d LEFT JOIN progress p ON p.document_id=d.id
"""


def document_json(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "filename": row["filename"],
        "title": row["title"],
        "size_bytes": row["size_bytes"],
        "created_at": row["created_at"],
        "last_opened_at": row["last_opened_at"],
        "progress": {
            "anchor": row["anchor"],
            "offset": row["offset"],
            "percent": row["percent"],
            "updated_at": row["progress_updated_at"],
        },
    }


def get_document(conn: sqlite3.Connection, document_id: str) -> sqlite3.Row:
    if DOCUMENT_ID.fullmatch(document_id) is None:
        raise HTTPException(404, "文件不存在")
    row = conn.execute(DOCUMENT_SELECT + " WHERE d.id=?", (document_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "文件不存在")
    return row


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings)
    limiter = LoginLimiter(settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        store.initialize()
        yield

    application = FastAPI(title="墨读", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    application.state.settings = settings
    application.state.store = store
    application.add_middleware(RequestSizeMiddleware, settings=settings)
    application.add_middleware(SecurityMiddleware, settings=settings)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": "请求参数不正确，请检查格式和取值范围"}, status_code=422)

    @application.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception):
        return JSONResponse({"detail": "服务器内部错误，请查看服务日志"}, status_code=500, headers={"Cache-Control": "no-store"})

    def optional_session(request: Request) -> Session | None:
        token = request.cookies.get(COOKIE_NAME, "")
        if re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
            return None
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        with store.connect() as conn:
            row = conn.execute("SELECT expires_at FROM sessions WHERE token_hash=?", (token_hash,)).fetchone()
            if row is None or row["expires_at"] <= time.time():
                if row:
                    conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
                return None
        return Session(token, token_hash)

    def require_session(session: Annotated[Session | None, Depends(optional_session)]) -> Session:
        if session is None:
            raise HTTPException(401, "请先登录")
        return session

    def require_csrf(request: Request, session: Annotated[Session, Depends(require_session)]) -> Session:
        supplied = request.headers.get("X-CSRF-Token", "")
        if re.fullmatch(r"[0-9a-f]{64}", supplied) is None or not hmac.compare_digest(supplied, store.csrf_token(session.token)):
            raise HTTPException(403, "会话校验失败，请刷新页面后重试")
        return session

    ReadSession = Annotated[Session, Depends(require_session)]
    WriteSession = Annotated[Session, Depends(require_csrf)]

    def session_json(session: Session | None) -> dict:
        return {
            "authenticated": session is not None,
            "csrf_token": store.csrf_token(session.token) if session else None,
            "max_upload_bytes": settings.max_upload_bytes,
        }

    @application.get("/api/health")
    def health():
        return {"status": "ok"}

    @application.get("/api/session")
    def current_session(response: Response, request: Request, session: Annotated[Session | None, Depends(optional_session)]):
        if session is None and request.cookies.get(COOKIE_NAME):
            response.delete_cookie(COOKIE_NAME, secure=settings.cookie_secure, httponly=True, samesite="strict")
        return session_json(session)

    @application.post("/api/login")
    def login(payload: LoginInput, request: Request, response: Response):
        address = request.client.host if request.client else "unknown"
        limiter.check(address)
        if not hmac.compare_digest(store.hash_password(payload.password), store.password_hash):
            raise HTTPException(401, "密码不正确")
        limiter.clear(address)
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        with store.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (time.time(),))
            old_token = request.cookies.get(COOKIE_NAME)
            if old_token and old_token.isascii():
                conn.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(old_token.encode("ascii")).hexdigest(),))
            conn.execute("INSERT INTO sessions(token_hash,expires_at) VALUES (?,?)", (token_hash, time.time() + settings.session_ttl_seconds))
        response.set_cookie(
            COOKIE_NAME, token, max_age=settings.session_ttl_seconds,
            secure=settings.cookie_secure, httponly=True, samesite="strict", path="/",
        )
        return session_json(Session(token, token_hash))

    @application.post("/api/logout", status_code=204)
    def logout(response: Response, session: WriteSession):
        with store.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (session.token_hash,))
        response.delete_cookie(COOKIE_NAME, secure=settings.cookie_secure, httponly=True, samesite="strict")
        response.status_code = 204
        return response

    @application.get("/api/documents")
    def list_documents(session: ReadSession):
        with store.connect() as conn:
            rows = conn.execute(DOCUMENT_SELECT + " ORDER BY COALESCE(d.last_opened_at,d.created_at) DESC, d.created_at DESC").fetchall()
        return {"documents": [document_json(row) for row in rows]}

    @application.post("/api/documents", status_code=201)
    async def upload_document(response: Response, session: WriteSession, file: UploadFile = File(...)):
        filename = file.filename or ""
        if (
            not filename or filename != filename.strip() or "/" in filename or "\\" in filename
            or "\x00" in filename or len(filename.encode("utf-8")) > 255
            or any(ord(character) < 32 or ord(character) == 127 for character in filename)
            or Path(filename).suffix.lower() not in {".md", ".markdown"}
        ):
            await file.close()
            raise HTTPException(400, "请上传文件名合法的 .md 或 .markdown 文件")
        try:
            raw = await file.read(settings.max_upload_bytes + 1)
        finally:
            await file.close()
        if len(raw) > settings.max_upload_bytes:
            raise HTTPException(413, "文件超过上传大小限制")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(400, "文件必须使用 UTF-8 编码") from None
        if "\x00" in content:
            raise HTTPException(400, "文件包含二进制内容，请上传纯文本 Markdown")
        if not content.replace("\ufeff", "").strip():
            raise HTTPException(400, "文件内容不能为空")
        digest = hashlib.sha256(raw).hexdigest()
        document_id = str(uuid.uuid4())
        path = store.file_path(document_id)
        temp_path = path.with_suffix(".tmp")
        created = False
        try:
            with store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(DOCUMENT_SELECT + " WHERE d.filename=? AND d.sha256=?", (filename, digest)).fetchone()
                if existing:
                    response.status_code = 200
                    return document_json(existing)
                with temp_path.open("xb") as stream:
                    temp_path.chmod(0o600)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                temp_path.replace(path)
                created = True
                conn.execute(
                    "INSERT INTO documents(id,filename,title,size_bytes,sha256,created_at) VALUES (?,?,?,?,?,?)",
                    (document_id, filename, title_from_markdown(content, filename), len(raw), digest, utc_now()),
                )
                result = document_json(get_document(conn, document_id))
        except BaseException:
            temp_path.unlink(missing_ok=True)
            if created:
                path.unlink(missing_ok=True)
            raise
        return result

    @application.get("/api/documents/{document_id}")
    def read_document(document_id: str, session: ReadSession):
        with store.connect() as conn:
            get_document(conn, document_id)
            try:
                content = store.file_path(document_id).read_bytes().decode("utf-8")
            except FileNotFoundError:
                raise HTTPException(404, "文件内容不存在，请从备份恢复") from None
            conn.execute("UPDATE documents SET last_opened_at=? WHERE id=?", (utc_now(), document_id))
            result = document_json(get_document(conn, document_id))
        return {**result, "content": content}

    @application.get("/api/documents/{document_id}/download")
    def download_document(document_id: str, session: ReadSession):
        with store.connect() as conn:
            row = get_document(conn, document_id)
        path = store.file_path(document_id)
        if not path.is_file():
            raise HTTPException(404, "文件内容不存在，请从备份恢复")
        return FileResponse(path, filename=row["filename"], media_type="text/markdown; charset=utf-8")

    @application.delete("/api/documents/{document_id}", status_code=204)
    def delete_document(document_id: str, session: WriteSession):
        path = store.file_path(document_id)
        trash = path.with_suffix(".trash")
        moved = False
        try:
            with store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                get_document(conn, document_id)
                if path.is_file():
                    path.replace(trash)
                    moved = True
                conn.execute("DELETE FROM documents WHERE id=?", (document_id,))
        except BaseException:
            if moved:
                trash.replace(path)
            raise
        trash.unlink(missing_ok=True)
        return Response(status_code=204)

    @application.put("/api/documents/{document_id}/progress")
    def update_progress(document_id: str, payload: ProgressInput, session: WriteSession):
        updated_at = utc_now()
        with store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            document = get_document(conn, document_id)
            if payload.client_id is not None:
                previous = conn.execute(
                    "SELECT sequence FROM progress_writes WHERE document_id=? AND client_id=?",
                    (document_id, payload.client_id),
                ).fetchone()
                if previous is not None and previous["sequence"] >= payload.sequence:
                    return document_json(document)["progress"]
                conn.execute(
                    "INSERT INTO progress_writes(document_id,client_id,sequence) VALUES (?,?,?) "
                    "ON CONFLICT(document_id,client_id) DO UPDATE SET sequence=excluded.sequence",
                    (document_id, payload.client_id, payload.sequence),
                )
            conn.execute(
                "INSERT INTO progress(document_id,anchor,offset,percent,updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(document_id) DO UPDATE SET anchor=excluded.anchor, offset=excluded.offset, "
                "percent=excluded.percent, updated_at=excluded.updated_at",
                (document_id, payload.anchor, payload.offset, payload.percent, updated_at),
            )
        return {**payload.model_dump(exclude={"client_id", "sequence"}), "updated_at": updated_at}

    @application.get("/api/preferences")
    def get_preferences(session: ReadSession):
        with store.connect() as conn:
            row = conn.execute("SELECT font_size,theme FROM preferences WHERE id=1").fetchone()
        return dict(row)

    @application.put("/api/preferences")
    def update_preferences(payload: PreferencesInput, session: WriteSession):
        with store.connect() as conn:
            conn.execute("UPDATE preferences SET font_size=?,theme=? WHERE id=1", (payload.font_size, payload.theme))
        return payload.model_dump()

    @application.api_route("/{path:path}", methods=["GET", "HEAD"])
    def frontend(path: str):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "接口不存在")
        static_root = settings.static_dir.resolve()
        target = (static_root / path).resolve()
        if not target.is_relative_to(static_root):
            raise HTTPException(404, "文件不存在")
        if target.is_file():
            cache = "public, max-age=31536000, immutable" if path.startswith("assets/") else "no-cache"
            return FileResponse(target, headers={"Cache-Control": cache})
        if Path(path).suffix:
            raise HTTPException(404, "文件不存在")
        index = static_root / "index.html"
        if not index.is_file():
            raise HTTPException(503, "前端尚未构建，请先运行 npm run build")
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return application


app = create_app()
