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


HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#080d19"><title>MailFlow AI — Email to Action</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700;800&family=Space+Grotesk:wght@500;600;700&display=swap');
:root{color-scheme:dark;--bg:#080d19;--panel:#101a2d;--panel2:#131f35;--line:rgba(160,181,224,.14);--text:#f3f6ff;--muted:#9aa9c4;--cyan:#65e6d2;--blue:#8b9cff;--violet:#b49aff;--green:#63dfad;--red:#ff8194}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:radial-gradient(ellipse at 80% -10%,rgba(105,119,255,.19),transparent 38%),radial-gradient(ellipse at 0% 40%,rgba(55,210,192,.09),transparent 35%),var(--bg);color:var(--text);font:15px 'DM Sans',system-ui,sans-serif;min-height:100vh}
body:before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.16;background-image:linear-gradient(rgba(255,255,255,.025) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.025) 1px,transparent 1px);background-size:42px 42px;mask-image:linear-gradient(to bottom,black,transparent 85%)}
main{position:relative;max-width:1120px;margin:auto;padding:26px 24px 55px}.topbar{display:flex;align-items:center;justify-content:space-between;gap:16px}.brand{display:flex;align-items:center;gap:11px;font:700 18px 'Space Grotesk';letter-spacing:.2px}.logo{width:38px;height:38px;border-radius:13px;display:grid;place-items:center;color:#07151c;background:linear-gradient(135deg,var(--cyan),#a4b0ff);box-shadow:0 7px 28px #6be6d333;font-size:22px}.live{display:flex;align-items:center;gap:8px;color:#b6c4da;font-size:12px;border:1px solid var(--line);background:#101a2d99;padding:9px 12px;border-radius:99px}.dot{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 13px var(--green);animation:pulse 2s infinite}
.hero{padding:78px 0 35px;max-width:800px;animation:rise .7s both}.eyebrow{color:var(--cyan);font-size:11px;font-weight:800;letter-spacing:2.4px;text-transform:uppercase;display:flex;align-items:center;gap:10px}.eyebrow:before{content:"";width:25px;height:1px;background:var(--cyan)}h1{font:700 clamp(40px,7vw,72px)/1.04 'Space Grotesk';letter-spacing:-3.5px;margin:19px 0 20px;max-width:800px}h1 span{background:linear-gradient(100deg,#fff 5%,#aebaff 52%,#6ee6d4);-webkit-background-clip:text;background-clip:text;color:transparent}.intro{color:var(--muted);font-size:clamp(16px,2vw,19px);line-height:1.7;max-width:670px;margin:0}.actions{display:flex;flex-wrap:wrap;gap:12px;margin-top:29px}.btn{border:0;border-radius:13px;padding:14px 20px;font:700 14px 'DM Sans';cursor:pointer;transition:transform .2s,box-shadow .2s,background .2s}.btn:hover{transform:translateY(-2px)}.primary{color:#08141b;background:linear-gradient(110deg,var(--cyan),#aab4ff);box-shadow:0 9px 28px #7ce7dc26}.primary:hover{box-shadow:0 12px 34px #7ce7dc42}.secondary{color:#e7edff;background:#18243a;border:1px solid var(--line)}.btn:disabled{opacity:.65;cursor:wait;transform:none}.spin{display:inline-block;animation:rotate .9s linear infinite}
.section-label{font:700 18px 'Space Grotesk';margin:35px 0 15px}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.card{background:linear-gradient(145deg,rgba(22,34,57,.96),rgba(14,24,42,.94));border:1px solid var(--line);border-radius:19px;padding:20px;box-shadow:0 12px 35px #00000015;transition:transform .25s,border-color .25s;animation:rise .65s both}.card:hover{transform:translateY(-4px);border-color:#8ca5e54d}.stat-head{display:flex;align-items:center;justify-content:space-between;color:var(--muted);font-size:12px;font-weight:600}.stat-icon{width:34px;height:34px;border-radius:11px;display:grid;place-items:center;background:#8b9cff1c;color:#aab7ff;font-size:16px}.num{font:700 32px 'Space Grotesk';margin-top:12px;letter-spacing:-1px}.sub{color:#8293b1;font-size:11px;margin-top:4px}.num.cyan{color:var(--cyan)}.num.green{color:var(--green)}.num.red{color:var(--red)}
.panel{background:rgba(15,25,44,.88);border:1px solid var(--line);border-radius:20px;overflow:hidden;box-shadow:0 18px 55px #00000018}.panel-head{padding:19px 21px;display:flex;align-items:center;justify-content:space-between;gap:10px;border-bottom:1px solid var(--line)}.panel-title{font:700 17px 'Space Grotesk'}.panel-hint{font-size:12px;color:var(--muted);margin-top:4px}.tag{font-size:11px;color:var(--cyan);border:1px solid #65e6d233;background:#65e6d20c;border-radius:99px;padding:7px 10px}.table-wrap{overflow:auto}table{width:100%;min-width:690px;border-collapse:collapse}th,td{padding:15px 18px;text-align:left;border-bottom:1px solid #a0b5e215;font-size:12px}th{color:#8e9fbd;font-size:10px;letter-spacing:1px;font-weight:800}td{color:#e4eaff}tbody tr{transition:background .2s}tbody tr:hover{background:#9aaeff0a}.muted{color:var(--muted)}.status{display:inline-flex;align-items:center;gap:6px;border-radius:99px;padding:6px 9px;background:#63dfad14;color:var(--green);font-size:10px;font-weight:700}.status.error{background:#ff819414;color:var(--red)}.empty{padding:30px!important;text-align:center;color:var(--muted)}
.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.step-no{font:700 12px 'Space Grotesk';color:var(--cyan)}.step-title{font:700 16px 'Space Grotesk';margin:13px 0 7px}.step-copy{color:var(--muted);font-size:13px;line-height:1.6;margin:0}.footer{display:flex;justify-content:space-between;gap:15px;flex-wrap:wrap;color:#8292ad;font-size:11px;padding:30px 2px 0}
.toast{position:fixed;z-index:10;left:50%;bottom:24px;transform:translate(-50%,20px);opacity:0;pointer-events:none;width:min(92vw,520px);padding:15px 17px;border:1px solid var(--line);border-radius:15px;background:#17243b;box-shadow:0 16px 50px #0008;color:#f4f6ff;transition:.25s;display:flex;gap:11px;align-items:flex-start}.toast.show{opacity:1;transform:translate(-50%,0)}.toast-icon{color:var(--cyan);font-size:17px}.toast strong{display:block;font-size:13px;margin-bottom:3px}.toast span{color:#aab8d1;font-size:12px;line-height:1.5}
@keyframes rise{from{opacity:0;transform:translateY(18px)}to{opacity:1;transform:translateY(0)}}@keyframes pulse{50%{opacity:.45;box-shadow:0 0 4px var(--green)}}@keyframes rotate{to{transform:rotate(360deg)}}
@media(max-width:760px){main{padding:19px 16px 38px}.hero{padding:61px 0 25px}h1{letter-spacing:-2px}.stats{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.card{padding:16px}.num{font-size:28px}.steps{grid-template-columns:1fr}.panel-head{padding:16px}.footer{padding-top:25px}}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{animation-duration:.01ms!important;animation-iteration-count:1!important;scroll-behavior:auto!important;transition-duration:.01ms!important}}
</style></head><body><main>
<header class="topbar"><div class="brand"><div class="logo">✳</div>MAILFLOW <span style="color:var(--cyan)">AI</span></div><div class="live"><i class="dot"></i><span id="connection">Dashboard ready</span></div></header>
<section class="hero"><div class="eyebrow">Intelligent email operations</div><h1>Turn your inbox into a <span>clear action plan.</span></h1><p class="intro">Find the follow-up hidden in every email. Extract the next action, deadline and owner, then move work into your task manager.</p><div class="actions"><button class="btn primary" id="run" onclick="pollNow()">↗ &nbsp;Run inbox check</button><button class="btn secondary" onclick="loadData()">↻ &nbsp;Refresh dashboard</button></div></section>
<div class="section-label">Workspace overview</div><section class="stats">
<div class="card"><div class="stat-head">Tasks created <span class="stat-icon">✓</span></div><div class="num cyan" id="created">—</div><div class="sub">Successfully routed</div></div>
<div class="card"><div class="stat-head">Emails reviewed <span class="stat-icon">✉</span></div><div class="num" id="reviewed">—</div><div class="sub">Recorded in activity</div></div>
<div class="card"><div class="stat-head">Task errors <span class="stat-icon">!</span></div><div class="num red" id="errors">—</div><div class="sub">Need attention</div></div>
<div class="card"><div class="stat-head">Destination <span class="stat-icon">⌁</span></div><div class="num" id="provider" style="font-size:21px">—</div><div class="sub">Connected task provider</div></div>
</section>
<div class="section-label">Recent activity</div><section class="panel"><div class="panel-head"><div><div class="panel-title">Email to task history</div><div class="panel-hint">Latest messages processed by MailFlow AI</div></div><span class="tag">LIVE DATA</span></div><div class="table-wrap"><table><thead><tr><th>EMAIL / SUBJECT</th><th>NEXT ACTION</th><th>DUE DATE</th><th>ASSIGNEE</th><th>STATUS</th></tr></thead><tbody id="rows"><tr><td class="empty" colspan="5">Loading activity…</td></tr></tbody></table></div></section>
<div class="section-label">How MailFlow works</div><section class="steps"><article class="card"><div class="step-no">01 / CONNECT</div><div class="step-title">Bring email together</div><p class="step-copy">Connect a mailbox through IMAP and review incoming messages from one workspace.</p></article><article class="card"><div class="step-no">02 / UNDERSTAND</div><div class="step-title">Find the next action</div><p class="step-copy">Identify follow-ups, useful context, deadlines and the person responsible.</p></article><article class="card"><div class="step-no">03 / DELIVER</div><div class="step-title">Create trackable tasks</div><p class="step-copy">Send actionable work to your configured task destination.</p></article></section>
<footer class="footer"><span>© MailFlow AI · Python + FastAPI</span><span>Automation workspace · Configure credentials before live inbox checks</span></footer></main>
<div class="toast" id="toast" role="status"><div class="toast-icon">✦</div><div><strong id="toastTitle">MailFlow AI</strong><span id="toastText"></span></div></div>
<script>
let toastTimer;function notify(title,message){const el=document.getElementById("toast");document.getElementById("toastTitle").textContent=title;document.getElementById("toastText").textContent=message;el.classList.add("show");clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove("show"),5200)}
function esc(v){return String(v||"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
async function loadData(){try{const [s,t]=await Promise.all([fetch("/api/status").then(r=>r.json()),fetch("/api/tasks").then(r=>r.json())]);const c=s.counts||{};document.getElementById("created").textContent=c.created||0;document.getElementById("reviewed").textContent=t.length;document.getElementById("errors").textContent=c.error||0;document.getElementById("provider").textContent=(s.provider||"demo").toUpperCase();document.getElementById("connection").textContent=s.email_configured?"Mailbox configured":"Setup required";document.getElementById("rows").innerHTML=t.length?t.map(x=>"<tr><td><b>"+esc(x.subject)+"</b><br><span class='muted'>"+esc(x.sender)+"</span></td><td>"+esc(x.action)+"</td><td>"+esc(x.due_date)+"</td><td>"+esc(x.assignee)+"</td><td><span class='status "+(x.status==="error"?"error":"")+"'>"+esc(x.status)+"</span></td></tr>").join(""):"<tr><td class='empty' colspan='5'>No activity yet. Configure your mailbox to begin.</td></tr>"}catch(e){document.getElementById("connection").textContent="API unavailable";notify("Could not refresh","The dashboard API could not be reached. Please try again.")}}
async function pollNow(){const b=document.getElementById("run");b.disabled=true;b.innerHTML="<span class='spin'>◌</span> &nbsp;Checking inbox…";try{const r=await fetch("/api/poll",{method:"POST"});const d=await r.json();if(r.ok){notify("Inbox check complete",d.processed+" message(s) processed.");await loadData()}else{const msg=String(d.detail||"Inbox check failed");if(msg.includes("IMAP_USERNAME")||msg.includes("IMAP_PASSWORD"))notify("Mailbox not connected","Add IMAP_USERNAME and IMAP_PASSWORD to your deployment environment, then restart the service.");else notify("Inbox check failed",msg)}}catch(e){notify("Connection problem","The API could not be reached. Check the service and try again.")}finally{b.disabled=false;b.innerHTML="↗ &nbsp;Run inbox check"}}
loadData();setInterval(loadData,20000);
</script></body></html>"""
