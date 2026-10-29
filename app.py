"""33bots DE CRM — schlankes CRM für deutsche Leads aus ClickUp.

Leads kommen automatisch aus einer ClickUp-Liste, das Team bearbeitet sie hier:
Status (unterschrieben / nicht unterschrieben), Zuständigkeit, Kontaktdaten, Kommentare.
Keine Logins und Passwörter — wer gerade arbeitet, wählt sich oben aus der Teamliste aus.
"""
import math
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta

import requests
from flask import Flask, abort, flash, jsonify, make_response, redirect, render_template, request, url_for
from flask_sqlalchemy import SQLAlchemy

import mailer
import pricing

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "crm-de-dev")
_db_url = os.environ.get("DATABASE_URL", "sqlite:///crm-de.db")
if _db_url.startswith("postgres://"):  # Railway/Heroku liefern das alte Schema
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
db = SQLAlchemy(app)

STATUS_OPEN = "nicht_unterschrieben"
STATUS_SIGNED = "unterschrieben"
STATUSES = {STATUS_OPEN: "Nicht unterschrieben", STATUS_SIGNED: "Unterschrieben"}


# ---------- MODELLE ----------

class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(200))  # für die Zuordnung aus ClickUp (Assignee-E-Mail)
    active = db.Column(db.Boolean, nullable=False, default=True)


class Lead(db.Model):
    __tablename__ = "leads"
    id = db.Column(db.Integer, primary_key=True)
    clickup_task_id = db.Column(db.String(64), unique=True, index=True)
    company = db.Column(db.String(200), nullable=False)
    contact_name = db.Column(db.String(200))
    contact_email = db.Column(db.String(200))
    contact_email2 = db.Column(db.String(200))  # zweite E-Mail (z. B. Assistenz, Buchhaltung)
    contact_phone = db.Column(db.String(60))
    location = db.Column(db.String(200))
    event_date = db.Column(db.Date)
    notes = db.Column(db.Text)  # Inhalt aus ClickUp / Formular
    status = db.Column(db.String(30), nullable=False, default=STATUS_OPEN, index=True)
    assigned_to = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    signed_at = db.Column(db.DateTime)
    price_eur = db.Column(db.Float)  # Angebotspreis netto aus dem Kalkulator (inkl. Anfahrt)
    price_note = db.Column(db.Text)  # Aufschlüsselung der Kalkulation
    mail_sent_at = db.Column(db.DateTime)  # automatische E-Mail an den Kunden (Rückruftermin erfragen)
    notified_at = db.Column(db.DateTime)  # Team über neuen Lead benachrichtigt
    form_token = db.Column(db.String(40), index=True)  # Link zum Rückruf-Formular
    form_filled_at = db.Column(db.DateTime)
    callback_date = db.Column(db.Date)  # Wunschtermin für den Rückruf
    callback_slot = db.Column(db.String(40))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    assignee = db.relationship("User")

    @property
    def emails(self):
        """Gültige Adressen (Haupt-, dann zweite E-Mail), ohne Doppelte."""
        out = []
        for e in (self.contact_email, self.contact_email2):
            e = (e or "").strip()
            if e and _EMAIL_RE.fullmatch(e) and e.lower() not in [x.lower() for x in out]:
                out.append(e)
        return out

    comments = db.relationship("Comment", backref="lead", cascade="all, delete-orphan",
                               order_by="Comment.created_at.desc()")


class Comment(db.Model):
    __tablename__ = "comments"
    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    text = db.Column(db.Text, nullable=False)
    source = db.Column(db.String(20))  # None = Team, mail_in / mail_out = E-Mail-Verlauf, system
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    author = db.relationship("User")


class MailSeen(db.Model):
    """Schon übernommene E-Mails (Message-ID), damit nichts doppelt im Verlauf landet."""
    __tablename__ = "mail_seen"
    message_id = db.Column(db.String(320), primary_key=True)


class GeoCache(db.Model):
    """Gemerkte Koordinaten eingegebener Orte (Nominatim wird pro Ort nur einmal gefragt)."""
    __tablename__ = "geo_cache"
    query = db.Column(db.String(200), primary_key=True)
    lat = db.Column(db.Float)
    lon = db.Column(db.Float)
    label = db.Column(db.String(300))


class Setting(db.Model):
    __tablename__ = "settings"
    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text)


def get_setting(key, default=None):
    row = db.session.get(Setting, key)
    return row.value if row else default


def set_setting(key, value):
    row = db.session.get(Setting, key) or Setting(key=key)
    row.value = value
    db.session.add(row)


# ---------- HILFSFUNKTIONEN ----------

def current_user():
    """Ausgewählte Person aus dem Cookie (kein Login). None, solange niemand ausgewählt ist."""
    uid = request.cookies.get("crm_user", type=int) or request.cookies.get("posevo_user", type=int)
    user = db.session.get(User, uid) if uid else None
    return user if user and user.active else None


def active_users():
    return User.query.filter_by(active=True).order_by(User.name).all()


def parse_date(value):
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


@app.template_filter("dt")
def format_dt(value):
    return value.strftime("%d.%m.%Y %H:%M") if value else ""


@app.template_filter("d")
def format_d(value):
    return value.strftime("%d.%m.%Y") if value else ""


@app.context_processor
def inject_globals():
    open_count = Lead.query.filter(Lead.status == STATUS_OPEN).count() if request.endpoint != "health" else 0
    return {"me": current_user(), "team": active_users(), "STATUSES": STATUSES,
            "STATUS_OPEN": STATUS_OPEN, "STATUS_SIGNED": STATUS_SIGNED, "user_color": user_color,
            "open_count": open_count}


USER_COLORS = ["#6366f1", "#10b981", "#f59e0b", "#ec4899", "#3b82f6", "#ef4444", "#14b8a6", "#a855f7"]


def user_color(user):
    return USER_COLORS[(user.id - 1) % len(USER_COLORS)] if user else "#6b7080"


# ---------- CLICKUP-IMPORT ----------

CLICKUP_API = "https://api.clickup.com/api/v2"

# Teile von Feldnamen im ClickUp-Formular (klein geschrieben) -> Feld am Lead.
# Der Alias muss am Wortanfang stehen: "Ihre Telefonnummer" passt auf "telefon",
# "Ihre Antwort" aber nicht auf "ort".
CLICKUP_FIELD_ALIASES = {
    "company": ["firmenname", "firma", "unternehmen", "company", "nazwa firmy"],
    "contact_name": ["vor- und nachname", "ansprechpartner", "kontaktperson", "nachname", "vorname",
                     "name", "imię i nazwisko", "imie i nazwisko"],
    "contact_phone": ["telefon", "handy", "mobil", "phone", "tel"],
    "contact_email": ["e-mail", "email", "mail"],
    "location": ["veranstaltungsort", "ort", "stadt", "location", "lokalizacja", "miasto"],
    "event_date": ["veranstaltungsdatum", "datum", "termin", "date", "data"],
}

_PHONE_RE = re.compile(r"(?:\+|00)\d[\d\s/()-]{7,}\d|\b0\d[\d\s/-]{6,}\d\b")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


class ClickUpError(Exception):
    pass


def clickup_configured():
    return bool(os.environ.get("CLICKUP_API_TOKEN", "").strip() and os.environ.get("CLICKUP_LIST_ID", "").strip())


def _clickup_get(path, params=None):
    headers = {"Authorization": os.environ.get("CLICKUP_API_TOKEN", "").strip()}
    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(f"{CLICKUP_API}{path}", headers=headers, params=params, timeout=20)
        except requests.exceptions.RequestException as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
            continue
        if not r.ok:
            raise ClickUpError(f"ClickUp {r.status_code}: {r.text[:300]}")
        return r.json()
    raise ClickUpError(f"Keine Verbindung zu ClickUp: {last_err}")


def _custom_field_value(cf):
    """Lesbarer Wert eines Custom Fields (Dropdowns liefern nur den Index der Option)."""
    value = cf.get("value")
    if value in (None, "", []):
        return None
    if cf.get("type") == "drop_down":
        options = (cf.get("type_config") or {}).get("options") or []
        for opt in options:
            if value in (opt.get("orderindex"), opt.get("id")):
                return opt.get("name")
    if cf.get("type") == "date":
        try:
            return datetime.utcfromtimestamp(int(value) / 1000).date()
        except (TypeError, ValueError):
            return None
    if isinstance(value, (list, dict)):
        return None
    return str(value).strip()


def task_to_fields(task):
    """Übersetzt eine ClickUp-Aufgabe in Lead-Felder."""
    fields = {}
    extra = []
    for cf in task.get("custom_fields") or []:
        name = (cf.get("name") or "").strip()
        value = _custom_field_value(cf)
        if value in (None, ""):
            continue
        lname = name.lower()
        target = next((f for f, aliases in CLICKUP_FIELD_ALIASES.items()
                       if any(re.search(r"(?<![a-zäöüß])" + re.escape(a), lname) for a in aliases)), None)
        if target == "event_date":
            value = value if not isinstance(value, str) else parse_date(value)
            if value:
                fields.setdefault("event_date", value)
                continue
        elif target == "contact_email" and "contact_email" in fields and "contact_email2" not in fields \
                and str(value).strip().lower() != fields["contact_email"].lower():
            fields["contact_email2"] = str(value)[:200]  # zweites E-Mail-Feld im Formular
            continue
        elif target and target not in fields:
            fields[target] = str(value)[:200]
            continue
        extra.append(f"{name}: {value}")
    title = (task.get("name") or "").strip()
    if "company" not in fields:  # ohne Firmenfeld im Formular gilt der Aufgabentitel als Firma
        fields["company"] = (title or "Lead aus ClickUp")[:200]
    elif title and title != fields["company"] and "contact_name" not in fields:
        fields["contact_name"] = title[:200]  # Titel ist dann meist der Name der anfragenden Person
    text = (task.get("text_content") or "").strip()
    fields["notes"] = "\n".join(([text] if text else []) + extra).strip()
    blob = f"{task.get('name', '')}\n{text}"
    if "contact_phone" not in fields:
        m = _PHONE_RE.search(blob)
        if m:
            fields["contact_phone"] = m.group(0).strip()
    if "contact_email" not in fields:
        m = _EMAIL_RE.search(blob)
        if m:
            fields["contact_email"] = m.group(0)
    if "event_date" not in fields and task.get("due_date"):
        try:
            fields["event_date"] = datetime.utcfromtimestamp(int(task["due_date"]) / 1000).date()
        except (TypeError, ValueError):
            pass
    if task.get("date_created"):
        try:
            fields["created_at"] = datetime.utcfromtimestamp(int(task["date_created"]) / 1000)
        except (TypeError, ValueError):
            pass
    emails = {(a.get("email") or "").lower() for a in task.get("assignees") or []}
    for u in User.query.filter(User.email.isnot(None)).all():
        if u.email and u.email.lower() in emails:
            fields["assigned_to"] = u.id
            break
    return fields


def sync_clickup():
    """Holt neue Aufgaben aus der ClickUp-Liste und legt sie als Leads an.
    Bereits importierte Leads werden nicht überschrieben — nur leere Felder werden ergänzt."""
    if not clickup_configured():
        return {"ok": False, "info": "ClickUp nicht eingerichtet: CLICKUP_API_TOKEN und CLICKUP_LIST_ID setzen"}
    list_id = os.environ["CLICKUP_LIST_ID"].strip()
    new = 0
    page = 0
    while page <= 50:
        data = _clickup_get(f"/list/{list_id}/task", {"archived": "false", "page": page, "subtasks": "true"})
        tasks = data.get("tasks") or []
        for task in tasks:
            tid = task.get("id")
            if not tid:
                continue
            if get_setting(f"deleted:{tid}"[:64]):  # im CRM gelöscht — nicht erneut importieren
                continue
            fields = task_to_fields(task)
            lead = Lead.query.filter_by(clickup_task_id=tid).first()
            if lead:
                for f in ("contact_name", "contact_email", "contact_email2", "contact_phone", "location", "event_date"):
                    if not getattr(lead, f) and fields.get(f):
                        setattr(lead, f, fields[f])
                continue
            db.session.add(Lead(clickup_task_id=tid, status=STATUS_OPEN, **fields))
            new += 1
        if data.get("last_page") or not tasks:
            break
        page += 1
    set_setting("clickup_last_sync", datetime.utcnow().isoformat(timespec="seconds"))
    set_setting("clickup_last_error", "")
    db.session.commit()
    return {"ok": True, "new": new, "info": f"{new} neue Leads aus ClickUp importiert"}


_SYNC_LOCK = threading.Lock()


def sync_clickup_safe():
    if not _SYNC_LOCK.acquire(blocking=False):
        return {"ok": False, "info": "Synchronisierung läuft bereits"}
    try:
        return sync_clickup()
    except ClickUpError as e:
        db.session.rollback()
        return {"ok": False, "info": str(e)}
    except Exception as e:
        db.session.rollback()
        return {"ok": False, "info": f"Fehler bei der Synchronisierung: {type(e).__name__}: {e}"[:300]}
    finally:
        _SYNC_LOCK.release()


def _clickup_scheduler():
    interval = max(60, int(os.environ.get("CLICKUP_POLL_SECONDS", 300)))
    time.sleep(10)
    while True:
        with app.app_context():
            result = sync_clickup_safe()
            if not result.get("ok"):
                set_setting("clickup_last_error", result.get("info", ""))
                db.session.commit()
            db.session.remove()
        time.sleep(interval)


# ---------- SEITEN ----------

@app.route("/")
def leads():
    status = request.args.get("status", STATUS_OPEN)
    q = request.args.get("q", "").strip()
    who = request.args.get("who", "")
    query = Lead.query
    if status in STATUSES:
        query = query.filter(Lead.status == status)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Lead.company.ilike(like), Lead.contact_name.ilike(like),
                                    Lead.contact_email.ilike(like), Lead.contact_email2.ilike(like),
                                    Lead.contact_phone.ilike(like),
                                    Lead.location.ilike(like), Lead.notes.ilike(like)))
    if who == "none":
        query = query.filter(Lead.assigned_to.is_(None))
    elif who.isdigit():
        query = query.filter(Lead.assigned_to == int(who))
    rows = query.order_by(Lead.created_at.desc()).limit(500).all()
    counts = dict(db.session.query(Lead.status, db.func.count(Lead.id)).group_by(Lead.status).all())
    return render_template("leads.html", leads=rows, status=status, q=q, who=who, counts=counts,
                           total=sum(counts.values()), clickup_ok=clickup_configured(),
                           last_sync=get_setting("clickup_last_sync"), last_error=get_setting("clickup_last_error"),
                           mail_ok=mailer.configured(), mail_sync=get_setting("mail_last_sync"),
                           mail_error=get_setting("mail_sync_error"))


@app.route("/lead/<int:lead_id>")
def lead_detail(lead_id):
    lead = db.session.get(Lead, lead_id) or abort(404)
    return render_template("lead.html", lead=lead)


@app.route("/lead/neu", methods=["GET", "POST"])
def lead_new():
    if request.method == "GET":
        return render_template("lead_new.html")
    company = request.form.get("company", "").strip()
    if not company:
        flash("Bitte einen Firmennamen eingeben.")
        return redirect(url_for("lead_new"))
    lead = Lead(company=company[:200], status=STATUS_OPEN)
    _apply_form(lead, request.form)
    if lead.assigned_to is None and current_user():
        lead.assigned_to = current_user().id
    db.session.add(lead)
    db.session.commit()
    return redirect(url_for("lead_detail", lead_id=lead.id))


def _apply_form(lead, form):
    for f in ("company", "contact_name", "contact_email", "contact_email2", "contact_phone", "location"):
        if f in form:
            value = form.get(f, "").strip()[:200]
            if f == "company" and not value:
                continue
            setattr(lead, f, value or None)
    if "event_date" in form:
        lead.event_date = parse_date(form.get("event_date"))
    if "notes" in form:
        lead.notes = form.get("notes", "").strip() or None
    if "assigned_to" in form:
        uid = form.get("assigned_to", type=int)
        lead.assigned_to = uid if uid and db.session.get(User, uid) else None


@app.route("/lead/<int:lead_id>/bearbeiten", methods=["POST"])
def lead_edit(lead_id):
    lead = db.session.get(Lead, lead_id) or abort(404)
    _apply_form(lead, request.form)
    db.session.commit()
    flash("Gespeichert.")
    return redirect(url_for("lead_detail", lead_id=lead.id))


@app.route("/lead/<int:lead_id>/status", methods=["POST"])
def lead_status(lead_id):
    lead = db.session.get(Lead, lead_id) or abort(404)
    status = request.form.get("status")
    if status in STATUSES and status != lead.status:
        lead.status = status
        lead.signed_at = datetime.utcnow() if status == STATUS_SIGNED else None
        me = current_user()
        db.session.add(Comment(lead_id=lead.id, user_id=me.id if me else None,
                               text=f"Status geändert: {STATUSES[status]}"))
        db.session.commit()
    return redirect(request.form.get("next") or url_for("lead_detail", lead_id=lead.id))


@app.route("/lead/<int:lead_id>/kommentar", methods=["POST"])
def lead_comment(lead_id):
    lead = db.session.get(Lead, lead_id) or abort(404)
    text = request.form.get("text", "").strip()
    if text:
        me = current_user()
        db.session.add(Comment(lead_id=lead.id, user_id=me.id if me else None, text=text))
        db.session.commit()
    return redirect(url_for("lead_detail", lead_id=lead.id))


@app.route("/lead/<int:lead_id>/loeschen", methods=["POST"])
def lead_delete(lead_id):
    lead = db.session.get(Lead, lead_id) or abort(404)
    if lead.clickup_task_id:  # sonst käme er beim nächsten Import zurück
        set_setting(f"deleted:{lead.clickup_task_id}"[:64], "1")
    db.session.delete(lead)
    db.session.commit()
    flash("Lead gelöscht.")
    return redirect(url_for("leads"))


@app.route("/team", methods=["GET", "POST"])
def team():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if name:
            db.session.add(User(name=name[:80], email=(request.form.get("email", "").strip() or None)))
            db.session.commit()
        return redirect(url_for("team"))
    users = User.query.order_by(User.active.desc(), User.name).all()
    return render_template("team.html", users=users)


@app.route("/team/<int:uid>/aktiv", methods=["POST"])
def team_toggle(uid):
    user = db.session.get(User, uid) or abort(404)
    user.active = not user.active
    db.session.commit()
    return redirect(url_for("team"))


@app.route("/ich/<int:uid>")
def choose_user(uid):
    user = db.session.get(User, uid)
    target = request.args.get("next") or ""
    if not target.startswith("/") or target.startswith("//"):  # nur Seiten dieses CRM
        target = url_for("leads")
    resp = make_response(redirect(target))
    if user and user.active:
        resp.set_cookie("crm_user", str(user.id), max_age=60 * 60 * 24 * 365, samesite="Lax")
    return resp


@app.route("/clickup/sync", methods=["POST"])
def clickup_sync():
    result = sync_clickup_safe()
    flash(result["info"])
    return redirect(url_for("leads"))


# ---------- KALKULATOR ----------
# Wie im polnischen 33bots-CRM: Einsatzzeit (8/4/2 h), Tage, Startzeit (Nachtzuschlag), Abfahrt
# Warschau oder Kattowitz. Preise und Regeln in pricing.py; die Sätze sieht man im CRM nicht.
# Anfahrt zählt nur auf deutscher Seite: ab Grenzübergang bis zum Ort und zurück. Für jeden Start
# wählt der Rechner den Übergang mit der kürzesten Gesamtstrecke. Strecken: OSRM (OpenStreetMap),
# ohne Verbindung geschätzt (Luftlinie × 1,25).

ORIGINS = {
    "WAW": ("Warschau", 52.2297, 21.0122),
    "KTW": ("Kattowitz", 50.2649, 19.0238),
}
# Grenzübergänge nach Deutschland (Ortsmitte der deutschen Grenzstadt, auf wenige km genau)
CROSSINGS = [
    ("Frankfurt (Oder) / Świecko", 52.3420, 14.5506),
    ("Görlitz / Jędrzychowice", 51.1528, 14.9874),
    ("Forst (Lausitz) / Olszyna", 51.7358, 14.6390),
    ("Pomellen / Kołbaskowo", 53.3361, 14.3790),
    ("Waidhaus / Rozvadov (über Tschechien)", 49.6430, 12.4950),
]
ROAD_FACTOR = 1.25
OSM_HEADERS = {"User-Agent": "33bots-DE-CRM/1.0 (Leadverwaltung, Entfernungsrechner)"}


def _haversine_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def geocode(place):
    """(lat, lon, label) für einen Ort/eine Adresse in Deutschland oder None."""
    key = re.sub(r"\s+", " ", (place or "").strip().lower())[:200]
    if not key:
        return None
    row = db.session.get(GeoCache, key)
    if row:
        return (row.lat, row.lon, row.label) if row.lat is not None else None
    r = requests.get("https://nominatim.openstreetmap.org/search", headers=OSM_HEADERS, timeout=15,
                     params={"q": place, "format": "json", "limit": 1, "countrycodes": "de"})
    r.raise_for_status()
    hits = r.json()
    hit = (float(hits[0]["lat"]), float(hits[0]["lon"]), hits[0].get("display_name", place)[:300]) if hits else None
    db.session.add(GeoCache(query=key, lat=hit[0] if hit else None, lon=hit[1] if hit else None,
                            label=hit[2] if hit else None))
    db.session.commit()
    return hit


def _road_table(points):
    """Straßen-km zwischen allen Punkten (Matrix) über OSRM; None, wenn nicht erreichbar."""
    coords = ";".join(f"{lon:.5f},{lat:.5f}" for lat, lon in points)
    try:
        r = requests.get(f"https://router.project-osrm.org/table/v1/driving/{coords}", headers=OSM_HEADERS,
                         params={"annotations": "distance"}, timeout=20)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != "Ok":
            return None
        return [[(d / 1000.0 if d is not None else None) for d in row] for row in data["distances"]]
    except (requests.exceptions.RequestException, ValueError, KeyError):
        return None


def plan_routes(dest):
    """Für jeden Start: bester Grenzübergang (kürzeste Gesamtstrecke) und km ab Grenze."""
    origins = list(ORIGINS.items())
    points = [(o[1], o[2]) for _, o in origins] + [(c[1], c[2]) for c in CROSSINGS] + [dest]
    table = _road_table(points)
    estimated = table is None
    n_o, d_idx = len(origins), len(points) - 1

    def km(i, j):
        if table and table[i][j] is not None:
            return table[i][j]
        return _haversine_km(points[i], points[j]) * ROAD_FACTOR

    plans = {}
    for oi, (code, (oname, _, _)) in enumerate(origins):
        best = None
        for ci, crossing in enumerate(CROSSINGS):
            c_idx = n_o + ci
            pl, de = km(oi, c_idx), km(c_idx, d_idx)
            if best is None or pl + de < best["total_km"]:
                best = {"origin": oname, "crossing": crossing[0], "pl_km": pl, "de_km": de, "total_km": pl + de}
        plans[code] = best
    return plans, estimated


@app.template_filter("eur")
def format_eur(value):
    return "" if value is None else pricing.fmt_eur(value)


def _calc_params(src):
    hours = src.get("h", type=int) or 8
    if hours not in pricing.HOUR_FACTORS:
        hours = 8
    km_raw = (src.get("km") or "").replace(",", ".").strip()
    try:
        km = max(0.0, min(float(km_raw), 3000.0)) if km_raw else None
    except ValueError:
        km = None
    start = pricing.parse_start(src.get("od"))
    days = max(1, min(src.get("dni", type=int) or 1, pricing.MAX_DAYS))
    return hours, km, start, days


def calc_quotes(place, hours, km, start, days):
    """{'dest', 'estimated', 'unknown', 'best', 'options': {WAW/KTW: {crossing, de_km, quote}}}.
    'best' = günstigster Preis für den Kunden (Abfahrt Warschau oder Kattowitz — wird nicht angezeigt)."""
    night = pricing.night_hours(start, hours)
    out = {"dest": None, "estimated": False, "unknown": False, "options": {}}
    if km is not None:
        for code in pricing.DEPARTURES:
            out["options"][code] = {"label": pricing.DEPARTURES[code], "crossing": None, "de_km": km,
                                    "quote": pricing.quote(hours, km, night, days)}
        out["best"] = out["options"]["WAW"]
        return out
    dest = geocode(place)
    if not dest:
        out["unknown"] = True
        return out
    plans, out["estimated"] = plan_routes(dest[:2])
    out["dest"] = dest
    for code, plan in plans.items():
        out["options"][code] = {"label": plan["origin"], "crossing": plan["crossing"], "de_km": plan["de_km"],
                                "quote": pricing.quote(hours, plan["de_km"], night, days)}
    out["best"] = min(out["options"].values(), key=lambda o: (o["quote"]["rounded"], o["de_km"]))
    return out


@app.route("/kalkulator")
def calculator():
    lead = db.session.get(Lead, request.args.get("lead", type=int) or 0)
    hours, km, start, days = _calc_params(request.args)
    place = (request.args.get("ort") or "").strip()[:200]
    if lead and not place and "ort" not in request.args:
        place = lead.location or ""
    result, error = None, None
    if place or km is not None:
        try:
            result = calc_quotes(place, hours, km, start, days)
            if result["unknown"]:
                error = f"Ort „{place}“ nicht gefunden — bitte PLZ und Ort angeben oder km ab Grenze eintragen."
                result = None
        except requests.exceptions.RequestException as e:
            error = f"Kartendienst nicht erreichbar ({type(e).__name__}) — km ab Grenze bitte manuell eintragen."
    return render_template(
        "calculator.html", lead=lead, place=place, hours=hours, km=km, start=start, days=days,
        start_text=pricing.fmt_hour(start) if start is not None else "",
        end_text=pricing.fmt_hour(start + hours) if start is not None else "",
        night=pricing.night_hours(start, hours), result=result, error=error,
        hour_options=sorted(pricing.HOUR_FACTORS, reverse=True),
        day_options=[(d, pricing.days_label(d), pricing.day_discount(d)) for d in range(1, pricing.MAX_DAYS + 1)],
        night_from=pricing.NIGHT_FROM, crossings=CROSSINGS,
    )


@app.route("/kalkulator/speichern", methods=["POST"])
def calculator_save():
    """Speichert den Angebotspreis im Lead und eine Kurzfassung (ohne Sätze) als Kommentar."""
    lead = db.session.get(Lead, request.form.get("lead", type=int) or 0) or abort(404)
    hours, km, start, days = _calc_params(request.form)
    place = (request.form.get("ort") or "").strip()[:200]
    try:
        res = calc_quotes(place, hours, km, start, days)
    except requests.exceptions.RequestException:
        res = {"options": {}}
    opt = res.get("best")
    if not opt:
        flash("Preis konnte nicht berechnet werden — bitte erneut versuchen.")
        return redirect(url_for("calculator", lead=lead.id, ort=place, h=hours, dni=days))
    q = opt["quote"]
    via = f"Grenze {opt['crossing']} → " if opt["crossing"] else ""
    route = f"{via}{place or 'Ort'}, {q['km']} km ab Grenze"
    lead.price_eur = float(q["rounded"])
    lead.price_note = pricing.summary(q, route)
    me = current_user()
    db.session.add(Comment(lead_id=lead.id, user_id=me.id if me else None,
                           text=f"Kalkulation gespeichert: {lead.price_note}"))
    db.session.commit()
    flash(f"Preis {pricing.fmt_eur(q['rounded'])} im Lead gespeichert.")
    return redirect(url_for("lead_detail", lead_id=lead.id))


# ---------- E-MAIL: Kunden-Mail, Rückruf-Formular, Benachrichtigungen, Verlauf ----------
# Neue Leads bekommen automatisch eine E-Mail von o.latek@33bots.de: kurze Fragen zur Veranstaltung,
# vor allem — wann dürfen wir anrufen? Link zu einem 30-Sekunden-Formular (Wunschtag + Zeitfenster).
# Das Team bekommt eine E-Mail bei jedem neuen Lead, bei einem Rückrufwunsch und bei einer Antwort
# des Kunden. Der Schriftverkehr aus dem Postfach (Eingang + Gesendet) erscheint als Kommentare.
# Nur Leads, die nach dem Einschalten neu dazukommen; ältere per Knopf auf der Lead-Seite.

_MAIL_LOCK = threading.Lock()
MAIL_SINCE_KEY = "mail_since"
MAIL_SYNC_KEY = "mail_last_sync"
MAIL_POLL_SECONDS = 300
CALLBACK_SLOTS = {"vormittag": "9–12 Uhr", "mittag": "12–15 Uhr", "nachmittag": "15–18 Uhr", "egal": "jederzeit"}
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
_BASE = {"url": ""}


@app.before_request
def _remember_base():
    if not _BASE["url"]:
        _BASE["url"] = request.url_root.rstrip("/")


def public_base_url():
    return os.environ.get("PUBLIC_URL", "").strip().rstrip("/") or _BASE["url"]


def lead_form_url(lead):
    if not lead.form_token:
        lead.form_token = secrets.token_urlsafe(18)
    base = public_base_url()
    return f"{base}/r/{lead.form_token}" if base else ""


def callback_label(lead):
    if not lead.callback_date:
        return ""
    d = lead.callback_date
    return f"{WEEKDAYS[d.weekday()]}, {d:%d.%m.%Y}, {CALLBACK_SLOTS.get(lead.callback_slot, lead.callback_slot or '')}".rstrip(", ")


def customer_mail(lead):
    link = lead_form_url(lead)
    greet = f"Guten Tag {lead.contact_name}," if lead.contact_name else "Guten Tag,"
    subject = "Ihre Anfrage: humanoider Roboter für Ihre Veranstaltung – 33bots"
    ask = (f"Wann passt Ihnen ein kurzer Rückruf? Hier können Sie in 30 Sekunden einen Termin wählen:\n{link}\n\n"
           "Oder antworten Sie einfach auf diese E-Mail") if link else "Wann passt Ihnen ein kurzer Rückruf? Antworten Sie einfach auf diese E-Mail"
    body = (f"{greet}\n\n"
            "vielen Dank für Ihre Anfrage zu unserem humanoiden Roboter Unitree G1!\n\n"
            "Damit wir Ihnen ein passendes Angebot machen können, besprechen wir die Details gern kurz am Telefon. "
            f"{ask} — mit Ihrem Wunschtermin und, falls schon bekannt, Datum und Ort der Veranstaltung.\n\n"
            "Mit freundlichen Grüßen\n"
            "Oliwier Latek\n33bots\n" + mailer.config()["user"])
    return subject, body


def _add_comment(lead_id, text, source, when=None, user_id=None):
    db.session.add(Comment(lead_id=lead_id, user_id=user_id, text=text, source=source,
                           created_at=when or datetime.utcnow()))


def send_customer_mail(lead, user_id=None):
    if not lead.emails:
        return "Lead hat keine E-Mail-Adresse"
    subject, body = customer_mail(lead)
    msg_id, err = mailer.send(lead.emails, subject, body)
    if err:
        return err
    lead.mail_sent_at = datetime.utcnow()
    if msg_id:
        db.session.merge(MailSeen(message_id=msg_id[:320]))
    _add_comment(lead.id, f"✉️ An {', '.join(lead.emails)} · Betreff: {subject}\n\n{body}", "mail_out", user_id=user_id)
    db.session.commit()
    return None


def team_emails():
    """Empfänger der Benachrichtigungen: NOTIFY_EMAILS (Standard hi@posevo.de) + Team mit E-Mail."""
    extra = [e.strip() for e in os.environ.get("NOTIFY_EMAILS", "hi@posevo.de").split(",") if e.strip()]
    return list(dict.fromkeys(extra + [u.email for u in active_users() if u.email]))


def notify_team(subject, body):
    """Benachrichtigung ans Posevo-Team — erkennbar als Nachricht von 33bots."""
    if os.environ.get("NOTIFY", "1") == "0" or not mailer.configured():
        return None
    to = team_emails()
    if not to:
        return None
    footer = ("\n\n—\nDiese Benachrichtigung kommt von 33bots (33bots DE CRM, Oliwier Latek)."
              + (f"\nAlle Leads: {public_base_url()}" if public_base_url() else ""))
    return mailer.send(to, f"[33bots] {subject}", body + footer, from_name="33bots CRM")[1]


def lead_url(lead):
    base = public_base_url()
    return f"{base}/lead/{lead.id}" if base else f"/lead/{lead.id}"


def notify_new_lead(lead):
    lines = [f"Firma: {lead.company}"]
    for label, value in (("Ansprechpartner", lead.contact_name), ("Telefon", lead.contact_phone),
                         ("E-Mail", ", ".join(lead.emails)), ("Ort", lead.location),
                         ("Termin", lead.event_date.strftime("%d.%m.%Y") if lead.event_date else "")):
        if value:
            lines.append(f"{label}: {value}")
    if lead.notes:
        lines.append(f"\nAnfrage:\n{lead.notes[:1500]}")
    err = notify_team(f"Neuer Lead: {lead.company}", "\n".join(lines) + f"\n\nIm CRM öffnen: {lead_url(lead)}")
    if not err:
        lead.notified_at = datetime.utcnow()


def mail_tick():
    """Ein Durchlauf: Benachrichtigung + Kunden-Mail für neue Leads, dann Postfach-Verlauf."""
    if not mailer.configured():
        return
    since = get_setting(MAIL_SINCE_KEY)
    if not since:  # erster Start: ab jetzt — ältere Leads bleiben unberührt
        set_setting(MAIL_SINCE_KEY, datetime.utcnow().isoformat(timespec="seconds"))
        db.session.commit()
        return
    since_dt = datetime.fromisoformat(since)
    base_ok = bool(public_base_url())
    for lead in Lead.query.filter(Lead.created_at >= since_dt).order_by(Lead.created_at).limit(50).all():
        if base_ok and not lead.notified_at:
            notify_new_lead(lead)
            db.session.commit()
        auto = os.environ.get("AUTO_MAIL", "1") != "0"
        if auto and base_ok and not lead.mail_sent_at and lead.emails and lead.status == STATUS_OPEN:
            tries = int(get_setting(f"mail_tries:{lead.id}") or 0)
            if tries < 3 and send_customer_mail(lead):
                db.session.rollback()
                set_setting(f"mail_tries:{lead.id}", str(tries + 1))
                db.session.commit()
    sync_mailbox()


def sync_mailbox(days_back=None, stats=None):
    """E-Mails mit Kunden (Eingang + Gesendet) als Kommentare an die passenden Leads hängen.
    stats (dict) bekommt eine Zusammenfassung für die Anzeige nach dem manuellen Abgleich."""
    stats = stats if stats is not None else {}
    last = get_setting(MAIL_SYNC_KEY)
    if days_back:
        start = datetime.utcnow() - timedelta(days=days_back)
    else:
        start = (datetime.fromisoformat(last) - timedelta(days=2)) if last else datetime.utcnow() - timedelta(days=14)
    by_addr = {}
    for lead in Lead.query.all():
        for e in lead.emails:
            by_addr.setdefault(e.lower(), []).append(lead)
    if not by_addr:
        stats["info"] = "keine Leads mit E-Mail-Adresse"
        return 0
    added, errors = 0, []
    for folder in ("INBOX", "SENT"):
        try:
            msgs = mailer.fetch(folder, start.date())
        except Exception as e:
            errors.append(f"{'Posteingang' if folder == 'INBOX' else 'Gesendet'}: {type(e).__name__}: {e}"[:200])
            continue
        if folder == "SENT" and mailer.fetch.last_folder is None:
            errors.append("Ordner „Gesendet“ nicht gefunden — IMAP_SENT_FOLDER in Railway setzen")
        key = "eingang" if folder == "INBOX" else "gesendet"
        stats[key] = {"ordner": mailer.fetch.last_folder, "geprueft": len(msgs), "neu": 0,
                      "ohne_lead": 0, "schon_da": 0}
        for m in msgs:
            if db.session.get(MailSeen, m["message_id"]):
                stats[key]["schon_da"] += 1
                continue
            text = mailer.strip_reply(m["text"])
            if folder == "INBOX":
                leads = by_addr.get(m["from"], [])
                for lead in leads:
                    _add_comment(lead.id, f"✉️ Von {m['from']} · Betreff: {m['subject']}\n\n{text}", "mail_in", m["date"])
                if leads:
                    db.session.add(MailSeen(message_id=m["message_id"]))
                    notify_team(f"Antwort vom Kunden: {leads[0].company}",
                                f"{m['from']} schreibt ({m['subject']}):\n\n{text[:1500]}\n\nIm CRM öffnen: {lead_url(leads[0])}")
            else:
                # alles im Ordner „Gesendet“ ist von uns — egal mit welchem Absendernamen/Alias
                leads = list({l.id: l for a in m["to"] for l in by_addr.get(a, [])}.values())
                for lead in leads:
                    _add_comment(lead.id, f"✉️ An {', '.join(m['to'])} · Betreff: {m['subject']}\n\n{text}",
                                 "mail_out", m["date"])
                if leads:
                    db.session.add(MailSeen(message_id=m["message_id"]))
            if leads:
                added += 1
                stats[key]["neu"] += 1
            else:
                stats[key]["ohne_lead"] += 1
            # nicht zugeordnete Mails nicht als „gesehen“ markieren — kommt der Lead später, wird nachgeholt
    set_setting(MAIL_SYNC_KEY, datetime.utcnow().isoformat(timespec="seconds"))
    set_setting("mail_sync_error", " · ".join(errors))
    db.session.commit()
    return added


@app.route("/postfach/abgleichen", methods=["POST"])
def mailbox_sync_now():
    if not mailer.configured():
        flash("Postfach nicht eingerichtet (SMTP_HOST / SMTP_PASS in Railway).")
        return redirect(request.referrer or url_for("leads"))
    with _MAIL_LOCK:
        try:
            st = {}
            n = sync_mailbox(days_back=14, stats=st)
            err = get_setting("mail_sync_error")
            parts = []
            for key, label in (("eingang", "Posteingang"), ("gesendet", "Gesendet")):
                x = st.get(key)
                if x:
                    parts.append(f"{label} ({x['ordner'] or '—'}): {x['geprueft']} Mails der letzten 14 Tage, "
                                 f"{x['neu']} neu zugeordnet, {x['schon_da']} schon im Verlauf, {x['ohne_lead']} ohne passenden Lead")
            flash(f"Postfach abgeglichen: {n} neue E-Mails im Verlauf. " + " · ".join(parts)
                  + (f" · {st['info']}" if st.get("info") else "") + (f" · Problem: {err}" if err else ""))
        except Exception as e:
            db.session.rollback()
            flash(f"Postfach-Abgleich fehlgeschlagen: {type(e).__name__}: {e}"[:300])
    target = request.form.get("next") or ""
    return redirect(target if target.startswith("/") and not target.startswith("//") else url_for("leads"))


def _mail_scheduler():
    time.sleep(30)
    while True:
        if _MAIL_LOCK.acquire(blocking=False):
            try:
                with app.app_context():
                    mail_tick()
                    db.session.remove()
            except Exception:
                pass
            finally:
                _MAIL_LOCK.release()
        time.sleep(MAIL_POLL_SECONDS)


def mail_backlog():
    """Ältere offene Leads mit E-Mail, die noch keine Kunden-Mail bekommen haben."""
    return [l for l in Lead.query.filter(Lead.status == STATUS_OPEN, Lead.mail_sent_at.is_(None))
            .order_by(Lead.created_at).all() if l.emails]


_BACKLOG = {"running": False}
BACKLOG_PAUSE_SECONDS = 30  # Abstand zwischen den Mails — schont die Domain


@app.route("/kunden-mail/rueckstand", methods=["POST"])
def customer_mail_backlog():
    if not mailer.configured():
        flash("Postfach nicht eingerichtet (SMTP_HOST / SMTP_PASS).")
        return redirect(url_for("leads"))
    if _BACKLOG["running"]:
        flash("Der Versand läuft bereits.")
        return redirect(url_for("leads"))
    ids = [l.id for l in mail_backlog()]
    me = current_user()
    uid = me.id if me else None

    def run():
        _BACKLOG["running"] = True
        try:
            for i, lid in enumerate(ids):
                with app.app_context():
                    lead = db.session.get(Lead, lid)
                    if lead and not lead.mail_sent_at and lead.emails:
                        err = send_customer_mail(lead, user_id=uid)
                        if err:
                            db.session.rollback()
                            _add_comment(lid, f"⚠️ Kunden-E-Mail nicht gesendet: {err}", "system")
                            db.session.commit()
                    db.session.remove()
                if i < len(ids) - 1:
                    time.sleep(BACKLOG_PAUSE_SECONDS)
        finally:
            _BACKLOG["running"] = False
    threading.Thread(target=run, daemon=True).start()
    flash(f"Versand an {len(ids)} ältere Leads gestartet — eine E-Mail alle {BACKLOG_PAUSE_SECONDS} Sekunden "
          f"(ca. {max(1, len(ids) * BACKLOG_PAUSE_SECONDS // 60)} Min.). Gesendete Mails erscheinen in den Kommentaren.")
    return redirect(url_for("leads"))


@app.route("/lead/<int:lead_id>/kunden-mail", methods=["POST"])
def lead_customer_mail(lead_id):
    lead = db.session.get(Lead, lead_id) or abort(404)
    me = current_user()
    err = send_customer_mail(lead, user_id=me.id if me else None)
    if err:
        db.session.rollback()
    flash(f"E-Mail nicht gesendet: {err}" if err else "E-Mail an den Kunden gesendet.")
    return redirect(url_for("lead_detail", lead_id=lead.id))


@app.route("/r/<token>", methods=["GET", "POST"])
def callback_form(token):
    """Öffentliches Formular für den Kunden: Wunschtermin für den Rückruf (+ optional Eventdaten)."""
    lead = Lead.query.filter_by(form_token=token).first() if len(token) >= 16 else None
    if not lead:
        abort(404)
    today = datetime.utcnow().date()
    if request.method == "GET":
        return render_template("callback_form.html", lead=lead, done=False, slots=CALLBACK_SLOTS, today=today)
    f = request.form
    day = parse_date(f.get("day"))
    slot = f.get("slot") if f.get("slot") in CALLBACK_SLOTS else "egal"
    phone = (f.get("phone") or "").strip()[:60]
    event_day = parse_date(f.get("event_date"))
    place = (f.get("place") or "").strip()[:200]
    comment = (f.get("comment") or "").strip()[:800]
    lead.callback_date = day if day and day >= today else today
    lead.callback_slot = slot
    if phone and phone != (lead.contact_phone or ""):
        if lead.contact_phone:
            comment = (comment + f"\nRückrufnummer: {phone}").strip()
        else:
            lead.contact_phone = phone
    if event_day and not lead.event_date:
        lead.event_date = event_day
    if place and not lead.location:
        lead.location = place
    lead.form_filled_at = datetime.utcnow()
    parts = [f"📞 Rückrufwunsch: {callback_label(lead)}", f"Telefon: {phone or lead.contact_phone or '—'}"]
    if event_day:
        parts.append(f"Veranstaltung: {event_day:%d.%m.%Y}")
    if place:
        parts.append(f"Ort: {place}")
    if comment:
        parts.append(f"Kommentar: {comment}")
    text = "\n".join(parts)
    _add_comment(lead.id, text, "system")
    db.session.commit()
    lead_id = lead.id

    def _notify():
        with app.app_context():
            l = db.session.get(Lead, lead_id)
            notify_team(f"Rückrufwunsch: {l.company} – {callback_label(l)}", f"{text}\n\nIm CRM öffnen: {lead_url(l)}")
            db.session.remove()
    threading.Thread(target=_notify, daemon=True).start()
    return render_template("callback_form.html", lead=lead, done=True, slots=CALLBACK_SLOTS, today=today)


app.jinja_env.globals.update(callback_label=callback_label, mail_ready=mailer.configured, mail_backlog=mail_backlog,
                             backlog_running=lambda: _BACKLOG["running"])

@app.route("/health")
def health():
    db.session.execute(db.text("SELECT 1"))
    return jsonify({"ok": True})


# ---------- FEHLERSEITEN (statt nacktem „Not Found“) ----------
# Kunden (Rückruf-Formular /r/…) sehen eine freundliche Seite mit Kontaktadresse, das Team im CRM eine
# Seite mit Weg zurück zu den Leads.
from werkzeug.exceptions import HTTPException as _HTTPException

_ERR_TEXT = {
    "kunde": {404: ("🔗", "Dieser Link ist nicht mehr gültig",
                    "Das Formular ist nicht mehr verfügbar. Schreiben Sie uns einfach — wir helfen gern:"),
              500: ("🛠️", "Es ist ein Fehler aufgetreten",
                    "Bitte versuchen Sie es in ein paar Minuten erneut oder schreiben Sie uns:")},
    "team": {404: ("🔎", "Seite nicht gefunden", "Diese Adresse gibt es nicht oder der Eintrag wurde gelöscht."),
             500: ("🛠️", "Etwas ist schiefgelaufen", "Bitte gleich noch einmal versuchen. Wenn der Fehler bleibt, 33bots Bescheid geben."),
             "other": ("⚠️", "Das hat nicht geklappt", "Diese Aktion ist gerade nicht möglich.")},
}


def _error_page(code):
    if request.is_json or "application/json" in request.headers.get("Accept", ""):
        return jsonify({"error": code}), code
    kunde = request.path.startswith("/r/")
    texts = _ERR_TEXT["kunde" if kunde else "team"]
    icon, title, message = texts.get(code) or texts.get("other") or texts[500]
    return render_template(
        "error.html", lang="de", icon=icon, title=title, message=message,
        contact=mailer.config()["user"] if kunde else "",
        back_url=None if kunde else url_for("leads"), back_label="Zurück zu den Leads",
        code="" if kunde else f"Fehler {code}"), code


@app.errorhandler(_HTTPException)
def _http_error(e):
    if e.code and e.code < 400:
        return e
    return _error_page(e.code or 500)


@app.errorhandler(Exception)
def _server_error(e):
    if isinstance(e, _HTTPException):
        return _http_error(e)
    try:
        db.session.rollback()
    except Exception:
        pass
    app.logger.exception("Unbehandelter Fehler: %s", e)
    return _error_page(500)


def ensure_columns():
    """Neue Spalten in bestehenden Datenbanken nachziehen (create_all legt nur neue Tabellen an)."""
    wanted = {
        "leads": {"contact_email2": "VARCHAR(200)", "price_eur": "FLOAT", "price_note": "TEXT",
                  "mail_sent_at": "TIMESTAMP", "notified_at": "TIMESTAMP", "form_token": "VARCHAR(40)",
                  "form_filled_at": "TIMESTAMP", "callback_date": "DATE", "callback_slot": "VARCHAR(40)"},
        "comments": {"source": "VARCHAR(20)"},
    }
    insp = db.inspect(db.engine)
    with db.engine.begin() as conn:
        for table, cols in wanted.items():
            have = {c["name"] for c in insp.get_columns(table)}
            for col, typ in cols.items():
                if col not in have:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


with app.app_context():
    db.create_all()
    ensure_columns()

if clickup_configured() and os.environ.get("CLICKUP_AUTOSYNC", "1") != "0":
    threading.Thread(target=_clickup_scheduler, daemon=True).start()

if mailer.configured() and os.environ.get("MAIL_AUTOSYNC", "1") != "0":
    threading.Thread(target=_mail_scheduler, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=os.environ.get("FLASK_DEBUG") == "1")
