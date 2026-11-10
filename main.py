import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DATA_PATH = Path(__file__).resolve().parent / "data" / "content.json"
INBOX_PATH = DATA_PATH.parent / "inbox.json"
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RESEND_URL = "https://api.resend.com/emails"
LOCK = threading.Lock()
RATE_LOCK = threading.Lock()
RATE_BUCKETS: dict[str, list[float]] = {}
TOKEN_TTL_SECONDS = 60 * 60 * 8

SECRET = os.environ.get("AUTH_SECRET", "dev-secret-change-me")
DEMO_USER = "admin"
DEMO_PASSWORD = "proje2026"


def _setting(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _same(left: str, right: str) -> bool:
    left_bytes = left.encode()
    right_bytes = right.encode()
    if len(left_bytes) != len(right_bytes):
        return False
    return hmac.compare_digest(left_bytes, right_bytes)


def _credentials_match(username: str, password: str) -> bool:
    accepted = {(DEMO_USER, DEMO_PASSWORD)}
    accepted.add((_setting("ADMIN_USER", DEMO_USER), _setting("ADMIN_PASSWORD", DEMO_PASSWORD)))
    return any(_same(username, user) and _same(password, secret) for user, secret in accepted)


def _cors_origins() -> list[str]:
    origins = [
        "http://127.0.0.1:43127",
        "http://localhost:43127",
        "https://alisenol.org",
        "https://www.alisenol.org",
    ]
    extra = os.environ.get("CORS_ORIGINS", "")
    for item in extra.split(","):
        origin = item.strip().rstrip("/")
        if origin and origin not in origins:
            origins.append(origin)
    return origins


app = FastAPI(title="Proje Portalı API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class Section(BaseModel):
    slug: str
    title: str
    content: str


class SectionsResponse(BaseModel):
    sections: list[Section]


class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""


class LoginResponse(BaseModel):
    token: str


class ContentUpdate(BaseModel):
    content: str = Field(max_length=20_000)


class ContactRequest(BaseModel):
    name: str = ""
    email: str = ""
    message: str = ""


class ContactResponse(BaseModel):
    sent: bool
    detail: str


def _load() -> dict:
    if not DATA_PATH.exists():
        raise HTTPException(status_code=500, detail="İçerik dosyası bulunamadı.")
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


def _save(data: dict) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATA_PATH.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(DATA_PATH)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(payload: str) -> str:
    return hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def create_token(username: str) -> str:
    body = _b64(
        json.dumps(
            {"sub": username, "exp": int(time.time()) + TOKEN_TTL_SECONDS},
            separators=(",", ":"),
        ).encode()
    )
    return f"{body}.{_sign(body)}"


def read_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Oturum gerekli. Yeniden giriş yapın.")
    token = authorization.removeprefix("Bearer ").strip()
    body, separator, signature = token.partition(".")
    if not separator or not hmac.compare_digest(_sign(body), signature):
        raise HTTPException(status_code=401, detail="Oturum geçersiz. Yeniden giriş yapın.")
    try:
        payload = json.loads(_unb64(body))
        username = payload["sub"]
        expires_at = int(payload["exp"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Oturum geçersiz. Yeniden giriş yapın.")
    if expires_at < int(time.time()) or not isinstance(username, str):
        raise HTTPException(status_code=401, detail="Oturum süresi doldu. Yeniden giriş yapın.")
    return username


def require_user(authorization: str | None = Header(default=None)) -> str:
    return read_token(authorization)


@app.get("/")
@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/sections", response_model=SectionsResponse)
def list_sections() -> SectionsResponse:
    with LOCK:
        data = _load()
    return SectionsResponse(sections=data["sections"])


@app.post("/api/auth/login", response_model=LoginResponse)
def login(body: LoginRequest) -> LoginResponse:
    username = body.username.strip()
    password = body.password.strip()
    if not username or not password:
        raise HTTPException(status_code=400, detail="Kullanıcı adı ve şifre gerekli.")
    if not _credentials_match(username, password):
        raise HTTPException(status_code=401, detail="Kullanıcı adı veya şifre hatalı.")
    return LoginResponse(token=create_token(username))


@app.put("/api/sections/{slug}", response_model=Section)
def update_section(
    slug: str,
    body: ContentUpdate,
    _: str = Depends(require_user),
) -> Section:
    content = body.content.strip()
    with LOCK:
        data = _load()
        for section in data["sections"]:
            if section["slug"] == slug:
                section["content"] = content
                _save(data)
                return Section(**section)
    raise HTTPException(status_code=404, detail="Bölüm bulunamadı.")


def _single_line(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]


def _throttle(ip: str) -> None:
    now = time.time()
    with RATE_LOCK:
        hits = [stamp for stamp in RATE_BUCKETS.get(ip, []) if now - stamp < 60]
        if len(hits) >= 8:
            raise HTTPException(
                status_code=429,
                detail="Çok fazla deneme. Bir dakika sonra yeniden deneyin.",
            )
        hits.append(now)
        RATE_BUCKETS[ip] = hits


def _message_text(value: str, limit: int) -> str:
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    return text[:limit]


def _remember_locally(name: str, email: str, message: str) -> None:
    with LOCK:
        current: list[dict[str, str]] = []
        if INBOX_PATH.exists():
            current = json.loads(INBOX_PATH.read_text(encoding="utf-8"))
        current.append(
            {
                "name": name,
                "email": email,
                "message": message,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        INBOX_PATH.write_text(
            json.dumps(current, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _send_with_resend(name: str, email: str, message: str) -> None:
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    to_email = os.environ.get("CONTACT_TO_EMAIL", "").strip()
    from_email = os.environ.get(
        "CONTACT_FROM_EMAIL",
        "Proje Portalı <onboarding@resend.dev>",
    ).strip()
    if not api_key or not to_email:
        raise HTTPException(
            status_code=503,
            detail="Resend yapılandırılmamış. RESEND_API_KEY ve CONTACT_TO_EMAIL gerekli.",
        )

    payload = json.dumps(
        {
            "from": from_email,
            "to": [to_email],
            "reply_to": email,
            "subject": f"İletişim: {name}",
            "text": f"Ad soyad: {name}\nE-posta: {email}\n\nMesaj:\n{message}\n",
        }
    ).encode()
    request = urllib.request.Request(
        RESEND_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "proje-portali",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        message = "E-posta gönderilemedi."
        try:
            parsed = json.loads(raw)
            remote = parsed.get("message")
            if isinstance(remote, str) and remote.strip():
                message = f"E-posta gönderilemedi: {remote.strip()}"
        except json.JSONDecodeError:
            if exc.code in (401, 403):
                message = "Resend anahtarı geçersiz."
        raise HTTPException(status_code=502, detail=message) from exc
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=502, detail="E-posta servisine ulaşılamadı.") from exc


@app.post("/api/contact", response_model=ContactResponse)
def submit_contact(body: ContactRequest, request: Request) -> ContactResponse:
    client_ip = request.client.host if request.client else "unknown"
    _throttle(client_ip)

    name = _single_line(body.name, 80)
    email = _single_line(body.email, 254).lower()
    message = _message_text(body.message, 4_000)
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Ad soyad gerekli.")
    if not EMAIL_PATTERN.fullmatch(email):
        raise HTTPException(status_code=400, detail="Geçerli bir e-posta adresi girin.")
    if not message:
        raise HTTPException(status_code=400, detail="Mesaj gerekli.")

    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    to_email = os.environ.get("CONTACT_TO_EMAIL", "").strip()
    if not api_key or not to_email:
        _remember_locally(name, email, message)
        return ContactResponse(
            sent=False,
            detail="Mesaj kaydedildi. Resend anahtarı tanımlı olmadığı için e-posta gönderilmedi.",
        )

    _send_with_resend(name, email, message)
    return ContactResponse(sent=True, detail="Mesajınız gönderildi.")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8742")),
    )
