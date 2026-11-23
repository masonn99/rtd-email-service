"""Builds and sends the inquiry emails over Gmail SMTP."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Optional

from .config import ATTACHMENT_PATH, SMTP_HOST, SMTP_PORT

SUBJECT = "Inquiry: entry requirements for US Refugee Travel Document holders - {country}"

BODY = """Dear Consular Section of the Embassy of {country},

{opening}

Could you please let me know:

1. Does {country} recognize and accept the US Refugee Travel Document (Form I-571) for entry?
2. Is a visa required? If not, how long may a holder of this document stay?
3. If a visa is required, can it be obtained on arrival, online (e-visa), or must it be applied for at an embassy or consulate?

For reference, I have attached the US Customs and Border Protection information sheet describing this document.

Thank you very much for your help.

Kind regards,
{sender}
"""

OPENINGS = {
    "initial": "I am writing to ask about the entry requirements for holders of a United States "
               "Refugee Travel Document (Form I-571), issued by USCIS.",
    "retry": "I am writing to ask about the entry requirements for holders of a United States "
             "Refugee Travel Document (Form I-571), issued by USCIS. I sent this question to another "
             "address earlier but did not receive a reply, so I am trying this address in case it "
             "is the right team.",
    "follow_up": "I am following up on my earlier email about the entry requirements for holders "
                 "of a United States Refugee Travel Document (Form I-571), issued by USCIS, in case "
                 "it did not reach you.",
}


def build(
    to: str,
    country: str,
    kind: str,
    sender_addr: str,
    sender_name: str,
    in_reply_to: Optional[str] = None,
    attachment: Path = ATTACHMENT_PATH,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"{sender_name} <{sender_addr}>"
    msg["To"] = to
    subject = SUBJECT.format(country=country)
    msg["Subject"] = f"Re: {subject}" if kind == "follow_up" and in_reply_to else subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=sender_addr.split("@")[-1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(BODY.format(country=country, opening=OPENINGS[kind], sender=sender_name))
    if attachment.exists():
        msg.add_attachment(
            attachment.read_bytes(), maintype="application", subtype="pdf", filename=attachment.name
        )
    return msg


class Mailer:
    """Gmail SMTP sender; connects on the first send."""

    def __init__(self, user: str, password: str):
        self.user = user
        self.password = password
        self._smtp: Optional[smtplib.SMTP] = None

    def send(self, msg: EmailMessage) -> None:
        if self._smtp is None:
            self._smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60)
            self._smtp.starttls()
            self._smtp.login(self.user, self.password)
        self._smtp.send_message(msg)

    def close(self) -> None:
        if self._smtp is not None:
            try:
                self._smtp.quit()
            except smtplib.SMTPException:
                pass
            self._smtp = None
