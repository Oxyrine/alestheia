"""stdlib SMTP. Refuses real domains: all demo data is synthetic."""
import logging
import os
import smtplib
from collections.abc import Mapping
from email.message import EmailMessage
from email.utils import parseaddr

log = logging.getLogger(__name__)


def send(to: str, subject: str, body: str, env: Mapping[str, str] = os.environ) -> None:
    address = parseaddr(to)[1]
    if address != to or "@" not in address:
        raise ValueError("recipient must be one bare address")
    if env.get("ALLOW_REAL_DOMAINS") != "1" and not address.rsplit("@", 1)[1].lower().endswith(".example"):
        raise ValueError("refusing a non-.example recipient")

    message = EmailMessage()  # rejects newlines in headers, so a subject cannot inject recipients
    message["From"] = env.get("SMTP_FROM") or env.get("SMTP_USER") or "accounts@supplier.example"
    message["To"] = address
    message["Subject"] = subject
    message.set_content(body)

    if env.get("MAIL_MODE", "log") != "smtp":
        log.info("MAIL_MODE=log: message to %s recorded, not sent", address)
        return
    with smtplib.SMTP(env["SMTP_HOST"], int(env.get("SMTP_PORT") or 587), timeout=20) as smtp:
        if env.get("SMTP_STARTTLS", "1") == "1":  # set 0 only for a local Mailpit
            smtp.starttls()
        if env.get("SMTP_USER"):
            smtp.login(env["SMTP_USER"], env["SMTP_PASS"])
        smtp.send_message(message)
