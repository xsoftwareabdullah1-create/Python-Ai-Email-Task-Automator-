import asyncio
import email
import hashlib
import imaplib
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dateutil import parser as date_parser
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mailflow")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    poll_interval_seconds: int = 60
    task_provider: str = "demo"
    trello_key: str = ""
    trello_token: str = ""
    trello_list_id: str = ""
    asana_access_token: str = ""
    asana_project_gid: str = ""
    asana_default_assignee_gid: str = ""
    webhook_url: str = ""
    default_assignee: str = "Unassigned"
    default_due_days: int = 2
    timezone: str = "Asia/Kolkata"
    database_path: str = "data/automator.db"
    auto_start_worker: bool = False


cfg = Settings()
Path(cfg.database_path).parent.mkdir(parents=True, exist_ok=True)
SCHEMA = """CREATE TABLE IF NOT EXISTS emails (
 id TEXT PRIMARY KEY, subject TEXT, sender TEXT, received_at TEXT,
 needs_followup INTEGER, action TEXT, due_date TEXT, assignee TEXT,
 message_url TEXT, task_id TEXT, status TEXT, summary TEXT, created_at TEXT
)"""


def connect():
    con = sqlite3.connect(cfg.database_path)
    con.row_factory = sqlite3.Row
    con.execute(SCHEMA)
    return con


def decode_header_value(value):
    if not value:
        return ""
    result = []
    for part, encoding in decode_header(value):
        result.append(part.decode(encoding or "utf-8", errors="replace") if isinstance(part, bytes) else part)
    return "".join(result)


def get_body(message):
    pieces = []
    for part in message.walk():
        if part.get_content_type() == "text/plain" and part.get_content_disposition() != "attachment":
            try:
                pieces.append(part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace"))
            except Exception:
                pass
    if not pieces and not message.is_multipart():
        try:
            pieces.append(message.get_payload(decode=True).decode(message.get_content_charset() or "utf-8", errors="replace"))
        except Exception:
            pass
    return "\n".join(pieces).strip()[:25000]


ACTION_TERMS = re.compile(r"\b(please|kindly|action required|follow.?up|respond|reply|review|approve|send|share|prepare|complete|submit|confirm|schedule)\b", re.I)
DEADLINE = re.compile(r"\b(today|tomorrow|this week|next week|in \d+ days?|by [A-Za-z]+ \d{1,2}(?:,? \d{4})?|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b", re.I)


def extract_due(text):
    now = datetime.now(ZoneInfo(cfg.timezone))
    match = DEADLINE.search(text)
    if not match:
        return (now + timedelta(days=cfg.default_due_days)).date().isoformat()
    phrase = match.group(0).lower()
    if phrase == "today":
        due = now
    elif phrase == "tomorrow":
        due = now + timedelta(days=1)
    elif phrase == "this week":
        due = now + timedelta(days=max(0, 4 - now.weekday()))
    elif phrase == "next week":
        due = now + timedelta(days=7)
    elif phrase.startswith("in "):
        due = now + timedelta(days=int(re.search(r"\d+", phrase).group()))
    else:
        try:
            due = date_parser.parse(re.sub(r"^by\s+", "", phrase), default=now)
        except (ValueError, OverflowError):
            due = now + timedelta(days=cfg.default_due_days)
    return due.date().isoformat()


def classify(subject, body):
    text = subject + "\n" + body
    needs = bool(ACTION_TERMS.search(text)) or "?" in subject or "?" in body[:1200]
    action = next((line.strip() for line in body.splitlines() if ACTION_TERMS.search(line)), "Review and respond to this email")
    action = re.sub(r"\s+", " ", action)[:220]
    owner = re.search(r"(?:assigned to|owner|responsible person|@)\s*[: ]\s*([A-Z][\w.-]+(?:\s+[A-Z][\w.-]+)?)", body, re.I)
    return {
        "needs_followup": needs,
        "action": action,
        "due_date": extract_due(text),
        "assignee": owner.group(1) if owner else cfg.default_assignee,
        "summary": re.sub(r"\s+", " ", body)[:400],
    }


def create_task(task):
    provider = cfg.task_provider.lower()
    if provider == "demo":
        return "demo-" + hashlib.sha1(task["email_id"].encode()).hexdigest()[:10]
    if provider == "webhook":
        if not cfg.webhook_url:
            raise RuntimeError("WEBHOOK_URL is required")
        response = requests.post(cfg.webhook_url, json=task, timeout=20)
        response.raise_for_status()
        return str(response.json().get("id", "webhook-created"))
    if provider == "trello":
        if not all([cfg.trello_key, cfg.trello_token, cfg.trello_list_id]):
            raise RuntimeError("Trello key, token and list ID are required")
        description = "{}\n\nFrom: {}\nDue: {}\n{}".format(task["action"], task["sender"], task["due_date"], task["summary"])
        response = requests.post("https://api.trello.com/1/cards", params={
            "key": cfg.trello_key, "token": cfg.trello_token, "idList": cfg.trello_list_id,
            "name": task["subject"][:160], "desc": description,
            "due": task["due_date"] + "T23:59:00.000Z",
        }, timeout=20)
        response.raise_for_status()
        return str(response.json()["id"])
    if provider == "asana":
        if not cfg.asana_access_token or not cfg.asana_project_gid:
            raise RuntimeError("Asana token and project GID are required")
        data = {"data": {"name": task["subject"][:160],
                "notes": "{}\nFrom: {}\n{}".format(task["action"], task["sender"], task["summary"]),
                "due_on": task["due_date"], "projects": [cfg.asana_project_gid]}}
        if cfg.asana_default_assignee_gid:
            data["data"]["assignee"] = cfg.asana_default_assignee_gid
        response = requests.post("https://app.asana.com/api/1.0/tasks", json=data,
            headers={"Authorization": "Bearer " + cfg.asana_access_token}, timeout=20)
        response.raise_for_status()
        return str(response.json()["data"]["gid"])
    raise RuntimeError("Unsupported TASK_PROVIDER: " + provider)


def process_message(uid, message):
    subject = decode_header_value(message.get("Subject")) or "(No subject)"
    sender = decode_header_value(message.get("From"))
    body = get_body(message)
    email_id = hashlib.sha256((uid + "|" + (message.get("Message-ID") or "") + "|" + sender).encode()).hexdigest()
    with connect() as con:
        if con.execute("SELECT 1 FROM emails WHERE id=?", (email_id,)).fetchone():
            return
    details = classify(subject, body)
    try:
        received = parsedate_to_datetime(message.get("Date")).isoformat()
    except Exception:
        received = datetime.utcnow().isoformat()
    task_id, status, message_url = None, "ignored", ""
    if details["needs_followup"]:
        task = {"email_id": email_id, "subject": subject, "sender": sender,
                "received_at": received, "message_url": message_url,
                "action": details["action"], "due_date": details["due_date"],
                "assignee": details["assignee"], "summary": details["summary"]}
        try:
            task_id, status = create_task(task), "created"
        except Exception:
            status = "error"
            log.exception("Task creation failed")
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO emails VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (email_id, subject, sender, received, int(details["needs_followup"]), details["action"],
             details["due_date"], details["assignee"], message_url, task_id, status,
             details["summary"], datetime.utcnow().isoformat()))


def poll_inbox():
    if not cfg.imap_username or not cfg.imap_password:
        raise RuntimeError("Configure IMAP_USERNAME and IMAP_PASSWORD in .env")
    client = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, timeout=30)
    try:
        client.login(cfg.imap_username, cfg.imap_password)
        result, _ = client.select(cfg.imap_folder)
        if result != "OK":
            raise RuntimeError("Cannot select IMAP folder " + cfg.imap_folder)
        result, data = client.uid("search", None, "UNSEEN")
        if result != "OK":
            raise RuntimeError("IMAP search failed")
        count = 0
        for uid in data[0].split():
            result, parts = client.uid("fetch", uid, "(RFC822)")
            if result == "OK" and parts and isinstance(parts[0], tuple):
                process_message(uid.decode(), email.message_from_bytes(parts[0][1]))
                count += 1
        return count
    finally:
        try:
            client.logout()
        except Exception:
            pass


async def worker_loop():
    while True:
        try:
            count = await asyncio.to_thread(poll_inbox)
            log.info("Processed %s unseen messages", count)
        except Exception:
            log.exception("Inbox polling failed")
        await asyncio.sleep(max(15, cfg.poll_interval_seconds))


@asynccontextmanager
async def lifespan(application):
    task = None
    if cfg.auto_start_worker and cfg.imap_username and cfg.imap_password:
        task = asyncio.create_task(worker_loop())
    yield
    if task:
        task.cancel()


app = FastAPI(title="MailFlow AI", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "provider": cfg.task_provider, "worker_enabled": cfg.auto_start_worker}


@app.get("/api/status")
def status():
    with connect() as con:
        rows = con.execute("SELECT status, COUNT(*) AS count FROM emails GROUP BY status").fetchall()
    return {"counts": {row["status"]: row["count"] for row in rows},
            "provider": cfg.task_provider, "worker_enabled": cfg.auto_start_worker,
            "email_configured": bool(cfg.imap_username and cfg.imap_password)}


@app.get("/api/tasks")
def tasks(limit: int = 100):
    with connect() as con:
        rows = con.execute("SELECT * FROM emails ORDER BY created_at DESC LIMIT ?", (min(max(limit, 1), 500),)).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/poll")
async def poll_now():
    try:
        return {"processed": await asyncio.to_thread(poll_inbox)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTML


HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MailFlow AI</title><style>
*{box-sizing:border-box}body{margin:0;background:#090e1b;color:#edf2ff;font:15px system-ui}main{max-width:1100px;margin:auto;padding:28px 20px}.brand{font-size:22px;font-weight:800;color:#9aaaff}h1{font-size:clamp(36px,6vw,62px);line-height:1.05;max-width:750px;margin:55px 0 16px;letter-spacing:-2px}.muted{color:#96a4bc}.eyebrow{color:#55d7a4;text-transform:uppercase;letter-spacing:2px;font-size:12px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:32px 0}.card{background:#121d30;border:1px solid #283650;border-radius:15px;padding:20px}.num{font-size:30px;font-weight:800;margin-top:8px}button{background:#9aaaff;border:0;border-radius:9px;padding:12px 17px;font-weight:700;cursor:pointer}.secondary{background:#1b2940;color:white;border:1px solid #34445e;margin-left:8px}.table{overflow:auto;border:1px solid #283650;border-radius:14px}table{width:100%;min-width:650px;border-collapse:collapse}td,th{padding:14px;text-align:left;border-bottom:1px solid #283650;font-size:13px}th{color:#96a4bc}.green{color:#55d7a4}.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:15px}footer{padding:40px 0;color:#96a4bc;font-size:12px}@media(max-width:700px){.cards{grid-template-columns:repeat(2,1fr)}.steps{grid-template-columns:1fr}}
</style></head><body><main><div class="brand">✳ MAILFLOW AI</div><div class="eyebrow" style="margin-top:65px">Intelligent email operations</div><h1>Turn your inbox into a clear action plan.</h1><p class="muted">Detect follow-ups, extract the next action and deadline, and route work into your task manager.</p><p><button onclick="pollNow()">Run inbox check</button><button class="secondary" onclick="loadData()">Refresh</button></p><section class="cards"><div class="card"><div class="muted">Tasks created</div><div class="num" id="created">—</div></div><div class="card"><div class="muted">Emails reviewed</div><div class="num" id="reviewed">—</div></div><div class="card"><div class="muted">Task errors</div><div class="num" id="errors">—</div></div><div class="card"><div class="muted">Destination</div><div class="num" id="provider" style="font-size:22px">—</div></div></section><h2>Recent activity</h2><div class="table"><table><thead><tr><th>EMAIL / SUBJECT</th><th>ACTION</th><th>DUE</th><th>ASSIGNEE</th><th>STATUS</th></tr></thead><tbody id="rows"><tr><td colspan="5">No activity yet. Configure .env and start the worker.</td></tr></tbody></table></div><h2 style="margin-top:40px">How it works</h2><section class="steps"><div class="card"><b>01 · Ingest</b><p class="muted">Connect Gmail or Outlook over IMAP.</p></div><div class="card"><b>02 · Understand</b><p class="muted">Detect actions, deadlines and owners.</p></div><div class="card"><b>03 · Deliver</b><p class="muted">Create tasks in Trello, Asana or a webhook.</p></div></section><footer>MailFlow AI · Python + FastAPI · Modular provider architecture<br>Demo mode is active until you configure a mailbox and task destination.</footer></main><script>
function esc(v){return String(v||"").replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]})}
async function loadData(){try{let s=await fetch("/api/status").then(r=>r.json()),t=await fetch("/api/tasks").then(r=>r.json()),c=s.counts||{};document.getElementById("created").textContent=c.created||0;document.getElementById("reviewed").textContent=t.length;document.getElementById("errors").textContent=c.error||0;document.getElementById("provider").textContent=(s.provider||"demo").toUpperCase();document.getElementById("rows").innerHTML=t.length?t.map(function(x){return "<tr><td><b>"+esc(x.subject)+"</b><br><span class='muted'>"+esc(x.sender)+"</span></td><td>"+esc(x.action)+"</td><td>"+esc(x.due_date)+"</td><td>"+esc(x.assignee)+"</td><td class='green'>"+esc(x.status)+"</td></tr>"}).join(""):"<tr><td colspan='5'>No activity yet.</td></tr>"}catch(e){}}
async function pollNow(){try{let r=await fetch("/api/poll",{method:"POST"}),d=await r.json();alert(r.ok?"Processed "+d.processed+" messages":d.detail)}catch(e){alert("API unavailable")}loadData()}loadData();setInterval(loadData,15000);
</script></body></html>"""
