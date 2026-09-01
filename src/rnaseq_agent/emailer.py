from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from typing import Any


def send_completion_email(notification: dict[str, Any], subject: str, body: str) -> bool:
    if not notification.get("email_enabled"):
        return False

    password_env = notification.get("password_env", "RNASEQ_AGENT_SMTP_PASSWORD")
    password = os.environ.get(password_env)
    if not password:
        raise RuntimeError(f"Missing SMTP password environment variable: {password_env}")

    message = EmailMessage()
    message["From"] = notification["smtp_user"]
    message["To"] = notification["recipient"]
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP(notification["smtp_host"], int(notification["smtp_port"]), timeout=30) as smtp:
        smtp.starttls()
        smtp.login(notification["smtp_user"], password)
        smtp.send_message(message)

    return True
