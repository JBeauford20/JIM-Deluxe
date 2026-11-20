"""
DSM alert emails. Sends over SMTP when the environment is configured
(SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM); otherwise every message is
queued in email_outbox with status 'queued' so nothing is silently lost and the
portal / an operator can see what is waiting. Every send attempt is recorded in
email_outbox either way (sent / failed / queued).
"""
import json
import os
import smtplib
from email.message import EmailMessage


def smtp_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM"))


def _smtp_send(to_email: str, subject: str, body: str):
    msg = EmailMessage()
    msg["From"] = os.environ["SMTP_FROM"]
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    pw = os.environ.get("SMTP_PASS", "")
    with smtplib.SMTP(host, port, timeout=20) as s:
        s.starttls()
        if user:
            s.login(user, pw)
        s.send_message(msg)


def send_or_queue(cur, to_email: str, subject: str, body: str, kind: str,
                  context: dict | None = None) -> str:
    """Send now if SMTP is configured, else queue. Returns the resulting status.
    `cur` is an open cursor inside the caller's transaction."""
    status, error = "queued", None
    if smtp_configured():
        try:
            _smtp_send(to_email, subject, body)
            status = "sent"
        except Exception as e:                      # record, never raise into the request
            status, error = "failed", str(e)[:500]
    cur.execute("""
        INSERT INTO email_outbox (to_email, subject, body, kind, context, status, error, sent_at)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, CASE WHEN %s = 'sent' THEN now() END)
    """, (to_email, subject, body, kind, json.dumps(context or {}, default=str), status, error, status))
    return status


def flush_outbox(cur, limit: int = 50) -> dict:
    """Retry queued/failed messages (called when SMTP comes online). Returns counts."""
    if not smtp_configured():
        return {"sent": 0, "skipped": "SMTP not configured"}
    cur.execute("""SELECT id, to_email, subject, body FROM email_outbox
                   WHERE status IN ('queued', 'failed')
                   ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED""", (limit,))
    rows = cur.fetchall()
    sent = failed = 0
    for r in rows:
        try:
            _smtp_send(r["to_email"], r["subject"], r["body"])
            cur.execute("UPDATE email_outbox SET status='sent', sent_at=now(), error=NULL WHERE id=%s",
                        (r["id"],))
            sent += 1
        except Exception as e:
            cur.execute("UPDATE email_outbox SET status='failed', error=%s WHERE id=%s",
                        (str(e)[:500], r["id"]))
            failed += 1
    return {"sent": sent, "failed": failed, "remaining_checked": len(rows)}
