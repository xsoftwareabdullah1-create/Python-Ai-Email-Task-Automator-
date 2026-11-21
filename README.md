# MailFlow AI — Email to Task Automator

A modular Python starter that monitors an IMAP inbox, detects follow-ups, extracts actions/deadlines and creates tasks in Trello, Asana or a webhook. Includes a FastAPI dashboard and SQLite history.

## Quick start

```bash
cp .env.example .env
# Add mailbox and task-manager credentials to .env
docker compose up --build -d
```
Open http://localhost:8000. Run logs with `docker compose logs -f`.

## Local setup
Python 3.11+:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app:app --reload
```
Set `AUTO_START_WORKER=true` for background polling.

## Providers
- IMAP: Gmail (`imap.gmail.com:993`) or Outlook (`outlook.office365.com:993`)
- Task destination: `demo`, `trello`, `asana`, or `webhook`
- See `.env.example` for credentials and default assignee settings.

## Production caveats
The included classifier is a baseline, not a proven 90% recall model; validate it against your labelled test set before promising accuracy. Generic IMAP does not provide universal browser message links, so provider-specific Gmail API / Microsoft Graph adapters are required for reliable email permalinks. Assignee mapping must be configured against actual workspace members. A free web host may sleep and use ephemeral storage, so it cannot guarantee unattended 24-hour processing. Add dashboard authentication before exposing it publicly.

Architecture: IMAP poller → normalizer/classifier → dedupe store → task adapter → dashboard/API. Add report and spreadsheet modules behind separate adapters.

The dashboard's demo mode is not a live inbox demonstration. Never commit `.env` or real credentials.

## License
MIT