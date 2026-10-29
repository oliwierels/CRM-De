"""Preisrechner 33bots DE: Einsatzzeit vor Ort + Anfahrt ab der deutschen Grenze.

Regeln (wie im polnischen 33bots-CRM, in Euro):
- 8 h vor Ort = Tagespreis (Standard 2500 € netto), 4 h = 6/8, 2 h = 4/8 davon,
- jede Stunde nach 22:00 (bis 6:00) zählt × 1,5 des Stundensatzes des Pakets,
- Anfahrt = km ab Grenzübergang bis zum Ort × 2 (hin und zurück) × Preis pro km,
- die Anfahrt darf höchstens 30 % des Preises sein — sonst steigt der Preis so weit,
  dass sie genau 30 % ausmacht,
- mehrere Tage: 2 Tage −10 %, ab 3 Tagen −15 % auf alles; das Team bleibt vor Ort
  (eine Anfahrt für den ganzen Einsatz),
- Übernachtung immer: eine Nacht pro Einsatztag,
- Abfahrt Warschau oder Kattowitz: gerechnet wird beides, genommen der günstigere Preis
  (die Abfahrt wird im CRM nicht gezeigt).

Angebotsposten (lines): Roboter-Miete, Nachtzuschlag, Anfahrt, Übernachtung, Rabatt — in runden
Beträgen (auf 10 €), Summe = Angebotspreis. Die Anfahrt mit Übernachtung ist nie mehr als 30 % —
was der Preis darüber hinaus steigt, steckt in der Miete, damit die Posten „menschlich“ aussehen
(Miete ist immer der größte Posten).

Die Sätze sind nicht im CRM sichtbar: Standardwerte hier, Änderungen über Railway-Variablen
(CALC_DAY_PRICE, CALC_KM_PRICE, CALC_LODGING, CALC_ROUND_TO).
"""
import math
import os

HOUR_FACTORS = {8: 8 / 8, 4: 6 / 8, 2: 4 / 8}
NIGHT_FROM, NIGHT_TO = 22, 6
NIGHT_MULT = 1.5
TRAVEL_MAX_SHARE = 0.30
DAY_DISCOUNTS = {2: 0.10, 3: 0.15}
MAX_DAYS = 7
TRIPS = 2

DEPARTURES = {"WAW": "Warschau", "KTW": "Kattowitz"}
DEPARTURES_FROM = {"WAW": "ab Warschau", "KTW": "ab Kattowitz"}


def _env_float(name, default):
    try:
        return float(os.environ.get(name, "").replace(",", ".") or default)
    except ValueError:
        return default


def rates():
    """Aktuelle Sätze (Railway-Variablen oder Standard). Nur intern — nicht im CRM anzeigen."""
    return {
        "day_price": _env_float("CALC_DAY_PRICE", 2500.0),
        "km_price": _env_float("CALC_KM_PRICE", 1.0),
        "lodging": _env_float("CALC_LODGING", 150.0),
        "round_to": _env_float("CALC_ROUND_TO", 100.0),
    }


def parse_start(text):
    """'20:30' / '20' / '20.30' -> 20.5 oder None."""
    t = str(text or "").strip().replace(".", ":").replace(",", ":")
    if not t:
        return None
    hh, _, mm = t.partition(":")
    try:
        h, m = int(hh), int(mm or 0)
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h + m / 60


def night_hours(start, hours):
    """Wie viele der `hours` Stunden ab `start` in die Nacht (22–6 Uhr) fallen."""
    if start is None:
        return 0.0
    end = start + hours
    total = 0.0
    for a, b in ((NIGHT_FROM - 24, NIGHT_TO), (NIGHT_FROM, NIGHT_TO + 24)):
        total += max(0.0, min(end, b) - max(start, a))
    return round(total, 2)


def fmt_hour(value):
    value %= 24
    return f"{int(value):02d}:{int(round((value - int(value)) * 60)):02d}"


def day_discount(days):
    return DAY_DISCOUNTS.get(min(days, max(DAY_DISCOUNTS)), 0.0) if days > 1 else 0.0


def quote(hours, de_km_one_way, night=0.0, days=1, r=None):
    """Preis für Stunden pro Tag (2/4/8), km ab Grenze (einfach), Nachtstunden pro Tag und Tage."""
    r = r or rates()
    if hours not in HOUR_FACTORS:
        raise ValueError("Einsatzzeit: 2, 4 oder 8 h")
    days = max(1, min(int(days or 1), MAX_DAYS))
    km = int(round(max(0.0, float(de_km_one_way or 0))))
    night = min(max(0.0, float(night or 0)), float(hours))
    base = round(r["day_price"] * HOUR_FACTORS[hours])
    hourly = base / hours
    night_extra = round(night * hourly * (NIGHT_MULT - 1))
    onsite = (base + night_extra) * days
    travel = round(km * TRIPS * r["km_price"])
    lodging = round(r["lodging"] * days)
    trip = travel + lodging
    subtotal = onsite + trip
    raise_by = 0
    if subtotal and trip > TRAVEL_MAX_SHARE * subtotal:
        lifted = math.ceil(trip / TRAVEL_MAX_SHARE)
        raise_by, subtotal = lifted - subtotal, lifted
    pct = day_discount(days)
    discount = round(subtotal * pct)
    total = subtotal - discount
    step = r["round_to"] if r["round_to"] > 0 else 1
    q = {
        "hours": hours, "days": days, "km": km, "night": night, "night_extra": night_extra,
        "travel": travel, "lodging": lodging, "nights": days, "lodging_rate": r["lodging"],
        "discount_pct": pct, "discount": discount, "raise_by": raise_by, "subtotal": subtotal, "total": total,
        "rounded": int(math.ceil(total / step) * step), "per_day": total / days,
    }
    q["lines"] = offer_lines(q)
    return q


def _r10(value):
    return int(round(value / 10.0)) * 10


def offer_lines(q):
    """Posten fürs Angebot in runden Beträgen; Summe = q['rounded']. Die Miete (auf 50 €) enthält eine
    eventuelle Preiserhöhung durch das Anfahrt-Limit; die Rundung auf den Angebotspreis landet in der
    Anfahrt (wenige Euro), damit die Miete ein glatter Betrag bleibt."""
    days, hours = q["days"], q["hours"]
    base_rental = q["subtotal"] - q["travel"] - q["lodging"] - q["night_extra"] * days
    rental = int(round(base_rental / 50.0)) * 50
    nightly = _r10(q["lodging_rate"])
    lodging = nightly * q["nights"]
    night = _r10(q["night_extra"] * days)
    discount = _r10(q["discount"])
    travel = q["rounded"] + discount - rental - lodging - night
    if travel < 0:  # theoretisch bei km = 0 und Rundung nach unten — dann trägt die Miete den Rest
        rental, travel = rental + travel, 0
    lines = [(f"Roboter-Miete ({days_label(days)} × {hours} h, inkl. Betreuung vor Ort)", rental)]
    if night:
        lines.append((f"Nachtzuschlag ({q['night']:g} h nach {NIGHT_FROM}:00{' pro Tag' if days > 1 else ''})".replace(".", ","), night))
    lines.append((f"Anfahrt und Rückfahrt ({q['km']} km ab Grenze)", travel))
    if lodging:
        lines.append((f"Übernachtung Team ({q['nights']} {'Nacht' if q['nights'] == 1 else 'Nächte'})", lodging))
    if discount:
        lines.append((f"Mehrtagesrabatt −{q['discount_pct'] * 100:.0f} %", -discount))
    return lines


def fmt_eur(value):
    return f"{int(round(value)):,}".replace(",", ".") + " €"


def days_label(n):
    return "1 Tag" if n == 1 else f"{n} Tage"


def summary(q, route=""):
    """Angebotsposten als Text (für Lead und Kommentar)."""
    head = f"{route}\n" if route else ""
    body = "\n".join(f"{label}: {fmt_eur(amount) if amount >= 0 else '−' + fmt_eur(-amount)}" for label, amount in q["lines"])
    return f"{head}{body}\nGesamt netto: {fmt_eur(q['rounded'])}"
