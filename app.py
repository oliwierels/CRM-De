"""Posevo CRM — schlankes CRM für deutsche Leads aus ClickUp.

Leads kommen automatisch aus einer ClickUp-Liste, das Team bearbeitet sie hier:
Status (unterschrieben / nicht unterschrieben), Zuständigkeit, Kontaktdaten, Kommentare.
Keine Logins und Passwörter — wer gerade arbeitet, wählt sich oben aus der Teamliste aus.
"""
import math
import os
import re
import threading
import time
from datetime import datetime

import requests
from flask import Flask, abort, flash, jsonify, make_response, redirect, render_template, request, url_for
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "posevo-dev")
_db_url = os.environ.get("DATABASE_URL", "sqlite:///posevo.db")
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    author = db.relationship("User")


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
    uid = request.cookies.get("posevo_user", type=int)
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
    return {"me": current_user(), "team": active_users(), "STATUSES": STATUSES,
            "STATUS_OPEN": STATUS_OPEN, "STATUS_SIGNED": STATUS_SIGNED}


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
                           last_sync=get_setting("clickup_last_sync"), last_error=get_setting("clickup_last_error"))


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
    target = request.args.get("next") or request.referrer or url_for("leads")
    resp = make_response(redirect(target))
    if user and user.active:
        resp.set_cookie("posevo_user", str(user.id), max_age=60 * 60 * 24 * 365, samesite="Lax")
    return resp


@app.route("/clickup/sync", methods=["POST"])
def clickup_sync():
    result = sync_clickup_safe()
    flash(result["info"])
    return redirect(url_for("leads"))


# ---------- KALKULATOR ----------
# Preis = Tage × Tagespreis + Anfahrt. Die Anfahrt zählt nur auf deutscher Seite: von der Grenze
# bis zum Veranstaltungsort und zurück. Wir starten in Warschau oder Kattowitz; der Kalkulator
# rechnet für jeden Start den kürzesten Weg über einen der Grenzübergänge und nimmt daraus die
# Kilometer ab Grenze. Strecken kommen von OSRM (OpenStreetMap), ohne Verbindung wird
# geschätzt (Luftlinie × 1,25).

ORIGINS = {
    "WAW": ("Warschau", 52.2297, 21.0122),
    "KTW": ("Kattowitz", 50.2649, 19.0238),
}
# Grenzübergänge nach Deutschland (Ortsmitte der deutschen Grenzstadt, auf wenige km genau)
CROSSINGS = [
    ("Frankfurt (Oder) / Świecko — A2/A12", 52.3420, 14.5506),
    ("Görlitz / Jędrzychowice — A4", 51.1528, 14.9874),
    ("Forst (Lausitz) / Olszyna — A18/A15", 51.7358, 14.6390),
    ("Pomellen / Kołbaskowo — A6/A11", 53.3361, 14.3790),
    ("Waidhaus / Rozvadov (über Tschechien) — A6", 49.6430, 12.4950),
]
CALC_DEFAULTS = {"day_price": 2500.0, "km_price": 1.0, "round_to": 50.0}
ROAD_FACTOR = 1.25
OSM_HEADERS = {"User-Agent": "Posevo-CRM/1.0 (Leadverwaltung, Entfernungsrechner)"}


def calc_settings():
    out = {}
    for key, default in CALC_DEFAULTS.items():
        try:
            out[key] = float(get_setting(f"calc_{key}", default))
        except (TypeError, ValueError):
            out[key] = default
    return out


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


def compute_price(days, day_price, de_km_one_way, km_price, round_to, extra=0.0):
    rental = days * day_price
    travel = 2 * de_km_one_way * km_price
    raw = rental + travel + extra
    total = math.ceil(raw / round_to) * round_to if round_to > 0 else raw
    return {"rental": rental, "travel": travel, "extra": extra, "raw": raw, "total": total,
            "per_day": total / days if days else total}


@app.template_filter("eur")
def format_eur(value):
    if value is None:
        return ""
    s = f"{value:,.0f}".replace(",", ".")
    return f"{s} €"


@app.route("/kalkulator", methods=["GET", "POST"])
def calculator():
    cfg = calc_settings()
    src = request.form if request.method == "POST" else request.args
    lead = db.session.get(Lead, src.get("lead", type=int)) if src.get("lead", type=int) else None
    form = {
        "place": src.get("place", (lead.location if lead else "") or "").strip(),
        "days": max(1, src.get("days", 1, type=int) or 1),
        "day_price": src.get("day_price", cfg["day_price"], type=float),
        "km_price": src.get("km_price", cfg["km_price"], type=float),
        "extra": src.get("extra", 0.0, type=float) or 0.0,
        "origin": src.get("origin", "auto"),
        "manual_km": src.get("manual_km", type=float),
    }
    result, error = None, None
    if form["place"] or form["manual_km"]:
        try:
            dest = geocode(form["place"]) if form["place"] else None
            if form["place"] and not dest and not form["manual_km"]:
                error = f"Ort „{form['place']}“ nicht gefunden — bitte PLZ und Ort angeben oder km manuell eintragen."
            else:
                plans, estimated = plan_routes(dest[:2]) if dest else ({}, False)
                if form["manual_km"]:
                    chosen_code, de_km = "manual", form["manual_km"]
                else:
                    chosen_code = form["origin"] if form["origin"] in plans else \
                        min(plans, key=lambda c: plans[c]["total_km"])
                    de_km = plans[chosen_code]["de_km"]
                price = compute_price(form["days"], form["day_price"], de_km, form["km_price"],
                                      cfg["round_to"], form["extra"])
                result = {"dest": dest, "plans": plans, "estimated": estimated, "chosen": chosen_code,
                          "de_km": de_km, "price": price}
        except requests.exceptions.RequestException as e:
            error = f"Kartendienst nicht erreichbar ({type(e).__name__}) — km ab Grenze bitte manuell eintragen."
    if result and lead and request.method == "POST" and request.form.get("save") == "1":
        p, plan = result["price"], result["plans"].get(result["chosen"])
        route = (f"{plan['origin']} → {plan['crossing']} → {form['place']}" if plan
                 else "km manuell eingetragen")
        lead.price_eur = p["total"]
        lead.price_note = (f"{form['days']} Tag(e) × {format_eur(form['day_price'])} = {format_eur(p['rental'])}\n"
                           f"Anfahrt ab Grenze: 2 × {result['de_km']:.0f} km × {form['km_price']:.2f} € = {format_eur(p['travel'])}\n"
                           + (f"Zuschlag: {format_eur(p['extra'])}\n" if p["extra"] else "")
                           + f"Route: {route}\nAngebotspreis netto (gerundet): {format_eur(p['total'])}")
        me = current_user()
        db.session.add(Comment(lead_id=lead.id, user_id=me.id if me else None,
                               text=f"Kalkulation gespeichert: {format_eur(p['total'])} netto inkl. Anfahrt\n{lead.price_note}"))
        db.session.commit()
        flash(f"Preis {format_eur(p['total'])} im Lead gespeichert.")
        return redirect(url_for("lead_detail", lead_id=lead.id))
    return render_template("calculator.html", form=form, result=result, error=error, lead=lead, cfg=cfg,
                           ORIGINS=ORIGINS)


@app.route("/einstellungen", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        for key in CALC_DEFAULTS:
            value = request.form.get(key, type=float)
            if value is not None and value >= 0:
                set_setting(f"calc_{key}", str(value))
        db.session.commit()
        flash("Einstellungen gespeichert.")
        return redirect(url_for("settings"))
    return render_template("settings.html", cfg=calc_settings(), crossings=CROSSINGS, origins=ORIGINS)


@app.route("/health")
def health():
    db.session.execute(db.text("SELECT 1"))
    return jsonify({"ok": True})


def ensure_columns():
    """Neue Spalten in bestehenden Datenbanken nachziehen (create_all legt nur neue Tabellen an)."""
    wanted = {"leads": {"contact_email2": "VARCHAR(200)", "price_eur": "FLOAT", "price_note": "TEXT"}}
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=os.environ.get("FLASK_DEBUG") == "1")
