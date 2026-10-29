"""E-Mail für das 33bots DE CRM: Versand über SMTP und Lesen des Postfachs über IMAP.

Postfach (Railway-Variablen): SMTP_HOST, SMTP_PORT (587 = STARTTLS, 465 = SSL), SMTP_USER
(Standard o.latek@33bots.de), SMTP_PASS; IMAP_HOST (Standard = SMTP_HOST), IMAP_PORT (993),
IMAP_SENT_FOLDER (sonst automatisch erkannt).
"""
import email
import email.policy
import imaplib
import os
import re
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parseaddr, parsedate_to_datetime

DEFAULT_USER = "o.latek@33bots.de"


def config():
    host = os.environ.get("SMTP_HOST", "").strip()
    return {
        "host": host,
        "port": int(os.environ.get("SMTP_PORT", "587") or 587),
        "user": os.environ.get("SMTP_USER", "").strip() or DEFAULT_USER,
        "password": os.environ.get("SMTP_PASS", "").strip(),
        "imap_host": os.environ.get("IMAP_HOST", "").strip() or host,
        "imap_port": int(os.environ.get("IMAP_PORT", "993") or 993),
        "sent_folder": os.environ.get("IMAP_SENT_FOLDER", "").strip(),
        "from_name": os.environ.get("MAIL_FROM_NAME", "").strip() or "Oliwier Latek | 33bots",
    }


def configured():
    c = config()
    return bool(c["host"] and c["password"])


def send(to_addrs, subject, body, reply_to=None):
    """Sendet eine Text-E-Mail. Gibt (Message-ID, None) oder (None, Fehlertext) zurück."""
    c = config()
    if not configured():
        return None, "Postfach nicht eingerichtet (SMTP_HOST / SMTP_PASS)"
    to_addrs = [a for a in to_addrs if a]
    if not to_addrs:
        return None, "keine Empfänger"
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((c["from_name"], c["user"]))
    msg["To"] = ", ".join(to_addrs)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=c["user"].split("@")[-1])
    raw = msg.as_bytes()
    try:
        if c["port"] == 465:
            with smtplib.SMTP_SSL(c["host"], c["port"], timeout=30) as s:
                s.login(c["user"], c["password"])
                s.sendmail(c["user"], to_addrs, raw)
        else:
            with smtplib.SMTP(c["host"], c["port"], timeout=30) as s:
                s.starttls()
                s.login(c["user"], c["password"])
                s.sendmail(c["user"], to_addrs, raw)
    except (smtplib.SMTPException, OSError) as e:
        return None, f"Versand fehlgeschlagen: {type(e).__name__}"
    _save_sent(raw)
    return msg["Message-ID"], None


def _sent_folder(im):
    c = config()
    if c["sent_folder"]:
        return c["sent_folder"]
    typ, boxes = im.list()
    names = []
    for raw in boxes or []:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        m = re.match(r'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$', line)
        if not m:
            continue
        name = m.group("name").strip().strip('"')
        if "\\Sent" in m.group("flags"):
            return name
        names.append(name)
    for guess in ("Sent", "INBOX.Sent", "Gesendet", "INBOX.Gesendet", "Sent Items", "Sent Messages"):
        if guess in names:
            return guess
    return None


def _save_sent(raw):
    """Kopie im Ordner „Gesendet“, damit der Verlauf auch im Mailprogramm vollständig ist."""
    if os.environ.get("IMAP_SAVE_SENT", "1") == "0":
        return
    c = config()
    try:
        im = imaplib.IMAP4_SSL(c["imap_host"], c["imap_port"], timeout=20)
        try:
            im.login(c["user"], c["password"])
            folder = _sent_folder(im)
            if folder:
                im.append(f'"{folder}"', "\\Seen", imaplib.Time2Internaldate(datetime.now(timezone.utc)), raw)
        finally:
            try:
                im.logout()
            except Exception:
                pass
    except Exception:
        pass


def _text_of(msg):
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
    if part.get_content_type() == "text/html":
        import html as _html
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
        text = _html.unescape(re.sub(r"<[^>]+>", " ", text))
    return text


_QUOTE_START = re.compile(
    r"^(>|-{2,} ?(Original|Ursprüngliche)|Am .{3,80}schrieb|On .{3,80}wrote|Von: |From: |W dniu .{3,80}pisze)",
    re.IGNORECASE)


def strip_reply(text, limit=3000):
    """Nur der neue Teil einer Antwort (ohne zitierten Verlauf und Signatur-Trenner)."""
    out = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        if _QUOTE_START.match(line.strip()):
            break
        if line.strip() == "--":
            break
        out.append(line.rstrip())
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return body[:limit]


def fetch(folder, since):
    """Nachrichten eines Ordners seit `since` (date): Liste von dicts
    {message_id, date, from, to (Liste), subject, text}."""
    c = config()
    out = []
    im = imaplib.IMAP4_SSL(c["imap_host"], c["imap_port"], timeout=30)
    try:
        im.login(c["user"], c["password"])
        if folder == "SENT":
            folder = _sent_folder(im)
            if not folder:
                return out
        typ, _ = im.select(f'"{folder}"', readonly=True)
        if typ != "OK":
            return out
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        typ, data = im.uid("search", None, "SINCE", f"{since.day:02d}-{months[since.month - 1]}-{since.year}")
        for uid in (data[0] or b"").split()[-300:]:
            typ, parts = im.uid("fetch", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
            if not raw:
                continue
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            try:
                when = parsedate_to_datetime(str(msg.get("Date"))).astimezone(timezone.utc).replace(tzinfo=None)
            except Exception:
                when = datetime.utcnow()
            out.append({
                "message_id": str(msg.get("Message-ID") or "").strip()[:320] or f"uid:{folder}:{uid.decode()}",
                "date": when,
                "from": parseaddr(str(msg.get("From") or ""))[1].lower(),
                "to": [a.lower() for _, a in getaddresses([str(msg.get("To") or ""), str(msg.get("Cc") or "")]) if a],
                "subject": str(msg.get("Subject") or "")[:300],
                "text": _text_of(msg),
            })
    finally:
        try:
            im.logout()
        except Exception:
            pass
    return out
