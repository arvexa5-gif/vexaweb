from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr, constr
from datetime import datetime
import io
import csv
import sqlite3
from typing import Optional
import os
import smtplib
import threading
from email.message import EmailMessage

DB_PATH = "prejoin.db"


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


app = FastAPI(title="Vexa Prejoin API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # allow local file:// and other dev origins
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"]
)


class PrejoinPayload(BaseModel):
    fullName: constr(min_length=3)
    email: EmailStr
    consent: bool


@app.on_event("startup")
def on_startup() -> None:
    conn = get_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prejoin_submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                consent INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                user_agent TEXT,
                ip TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


@app.post("/api/prejoin")
def create_prejoin(data: PrejoinPayload, request: Request):
    if not data.consent:
        raise HTTPException(status_code=400, detail="Consent required")

    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO prejoin_submissions
            (full_name, email, consent, created_at, user_agent, ip)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                data.fullName.strip(),
                data.email.lower().strip(),
                1 if data.consent else 0,
                datetime.utcnow().isoformat() + "Z",
                request.headers.get("user-agent", ""),
                request.client.host if request.client else None,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Email already registered")
    finally:
        conn.close()

    # Send confirmation email in background (best-effort)
    try:
        threading.Thread(
            target=_send_confirmation_email,
            args=(data.email, data.fullName),
            daemon=True,
        ).start()
    except Exception:
        # do not fail the request because of email
        pass

    return {"ok": True}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/prejoin")
def list_prejoin(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    q: Optional[str] = Query(None),
):
    search = (q or "").strip()
    where_clause = ""
    params: list[object] = []
    if search:
        where_clause = "WHERE full_name LIKE ? OR email LIKE ?"
        like = f"%{search}%"
        params.extend([like, like])

    count_sql = f"SELECT COUNT(*) AS c FROM prejoin_submissions {where_clause}"
    list_sql = f"""
        SELECT id, full_name, email, consent, created_at, user_agent, ip
        FROM prejoin_submissions
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
    """
    offset = (page - 1) * limit
    conn = get_db()
    try:
        cur = conn.execute(count_sql, params)
        total = int(cur.fetchone()[0])
        cur = conn.execute(list_sql, [*params, limit, offset])
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    return {"items": rows, "page": page, "pageSize": limit, "total": total}


@app.get("/api/prejoin/export.csv")
def export_prejoin_csv(q: Optional[str] = Query(None)):
    search = (q or "").strip()
    where_clause = ""
    params: list[object] = []
    if search:
        where_clause = "WHERE full_name LIKE ? OR email LIKE ?"
        like = f"%{search}%"
        params.extend([like, like])

    sql = f"""
        SELECT id, full_name, email, consent, created_at, user_agent, ip
        FROM prejoin_submissions
        {where_clause}
        ORDER BY created_at DESC
    """
    conn = get_db()
    try:
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
    finally:
        conn.close()

    sio = io.StringIO()
    writer = csv.writer(sio)
    writer.writerow(["id", "full_name", "email", "consent", "created_at", "user_agent", "ip"])
    for r in rows:
        writer.writerow([r["id"], r["full_name"], r["email"], r["consent"], r["created_at"], r["user_agent"], r["ip"]])
    sio.seek(0)
    filename = "prejoin_export.csv"
    return StreamingResponse(
        iter([sio.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _send_confirmation_email(to_email: str, full_name: str) -> None:
    host = os.environ.get("SMTP_HOST", "127.0.0.1")
    port = int(os.environ.get("SMTP_PORT", "1025"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    use_tls = os.environ.get("SMTP_TLS", "true").lower() in ("1", "true", "yes", "on")
    from_addr = os.environ.get("SMTP_FROM", "noreply@vexa.local")

    subject = "Vexa ön kaydınız alındı"
    body = (
        f"Merhaba {full_name},\n\n"
        "Ön kaydınızı aldık. Lansman ile ilgili ilk siz bilgilendirileceksiniz.\n\n"
        "Teşekkürler,\nVexa Ekibi"
    )

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            if use_tls and port in (587, 25):
                try:
                    smtp.starttls()
                except Exception:
                    pass
            if user and password:
                try:
                    smtp.login(user, password)
                except Exception:
                    pass
            smtp.send_message(msg)
    except Exception:
        # Swallow errors silently; email is best-effort in dev
        pass


@app.post("/api/test-email")
def send_test_email(to: EmailStr, name: Optional[str] = None):
    try:
        _send_confirmation_email(to, name or "Değerli Kullanıcı")
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


