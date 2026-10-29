# 33bots DE CRM

Prosty CRM do obsługi niemieckich leadów (zespół Posevo). Interfejs po niemiecku, wygląd jak polski CRM 33bots.

- Leady wpadają automatycznie z listy ClickUp (co 5 minut, plus przycisk „ClickUp abrufen”).
- Dwa statusy: **Nicht unterschrieben** (każdy nowy lead) i **Unterschrieben**.
- Osoba odpowiedzialna, dane kontaktowe (dwa adresy e-mail), termin, notatki i komentarze przy każdym leadzie.
- Kalkulator ceny jak w polskim CRM (opis niżej) — bez widocznych stawek.
- Bez logowania i haseł: każdy wybiera siebie z listy „Ich bin” w menu (zapisywane w cookie).
  Zespół dodaje się w zakładce **Team**.
- Bez funkcji AI i bez połączenia z API 33bots.

## Kalkulator

Pola jak w polskim kalkulatorze: miejsce, czas na miejscu (8 / 4 / 2 h), liczba dni, godzina rozpoczęcia,
opcjonalnie km od granicy wpisane ręcznie. Miejsca wyjazdu nie widać: kalkulator liczy z Warszawy
i z Katowic i bierze tańszą cenę dla klienta.

Wynik: cena netto + **pozycje do oferty** (Roboter-Miete, Nachtzuschlag, Anfahrt und Rückfahrt,
Übernachtung Team, Mehrtagesrabatt) w okrągłych kwotach, które sumują się do ceny. Przycisk kopiuje
pozycje, „Im Lead speichern” zapisuje cenę i pozycje w leadzie.

Zasady (`pricing.py`):
- 8 h = stawka dzienna, 4 h = 6/8, 2 h = 4/8 tej stawki,
- każda godzina po 22:00 (do 6:00) × 1,5 stawki godzinowej pakietu,
- dojazd liczy się tylko w Niemczech: km od przejścia granicznego do miejsca × 2 × stawka za km, regresywnie:
  pierwsze 150 km w całości, 150–400 km w 50%, powyżej 400 km w 15%;
  przejście (Frankfurt (Oder), Görlitz, Forst, Pomellen, Waidhaus przez Czechy) wybierane automatycznie,
- limit: dojazd + nocleg najwyżej (cena maks. za dzień − stawka dzienna) × dni — przy 8 h dzień kosztuje
  najwyżej 3400 € (`CALC_MAX_DAY`) bez względu na odległość (dopłata nocna dochodzi osobno);
  krzywa jest tak płaska, że 3400 € wychodzi dopiero ok. 750 km od granicy — zachód Niemiec to 3200–3300 €,
- nocleg ekipy zawsze: jedna noc na każdy dzień,
- kilka dni: 2 dni −10%, od 3 dni −15%; ekipa zostaje na miejscu (jeden dojazd),
- pozycje zaokrąglone: wynajem do 50 €, reszta do 10 €; różnica z zaokrąglenia ceny ląduje w dojeździe.

Stawek nie widać w CRM — ustawia się je zmiennymi w Railway (niżej). Kilometry: OpenStreetMap
(Nominatim + OSRM); gdy serwis nie odpowiada — szacunek (linia prosta × 1,25).

## E-maile: klient, powiadomienia, historia korespondencji

Gdy podpięta jest skrzynka **o.latek@33bots.de** (zmienne niżej), co 5 minut:

- **Mail do klienta** (po niemiecku) przy każdym nowym leadzie z adresem e-mail: podziękowanie, kilka pytań
  o event, przede wszystkim — **kiedy możemy zadzwonić**. Link do 30-sekundowego formularza `/r/<token>`
  (dzień + przedział godzin + telefon, szczegóły eventu opcjonalnie). Wybrany termin pojawia się przy leadzie
  („📞 Rückruf: Mi, 05.11.2026, 9–12 Uhr”) i na liście leadów.
- **Powiadomienie dla zespołu Posevo** (osoby z e-mailem w zakładce Team + `NOTIFY_EMAILS`): nowy lead,
  wybrany termin oddzwonienia, odpowiedź klienta na maila.
- **Historia korespondencji**: maile między o.latek@33bots.de a klientem (Odebrane i Wysłane, także pisane
  ręcznie w programie pocztowym) trafiają do komentarzy leada — bez cytowanej poprzedniej treści.
- Tylko leady dodane po podpięciu skrzynki; starszym można wysłać mail przyciskiem na karcie leada.

| Zmienna | Opis |
|---|---|
| `SMTP_HOST` | serwer poczty 33bots.de |
| `SMTP_PORT` | 587 (STARTTLS, domyślnie) albo 465 (SSL) |
| `SMTP_USER` | domyślnie `o.latek@33bots.de` |
| `SMTP_PASS` | hasło do skrzynki |
| `IMAP_HOST` / `IMAP_PORT` | jeśli inne niż SMTP / 993 |
| `PUBLIC_URL` | adres CRM (np. `https://web-production-218a3.up.railway.app`) — do linków w mailach |
| `NOTIFY_EMAILS` | opcjonalnie dodatkowe adresy powiadomień, po przecinku |
| `AUTO_MAIL=0` / `NOTIFY=0` | wyłącza mail do klienta / powiadomienia |

## Wdrożenie na Railway

1. Nowy projekt → Deploy from GitHub → to repo.
2. **+ Add → Database → PostgreSQL**, potem w serwisie aplikacji zmienna `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`.
3. Zmienne środowiskowe:

| Zmienna | Opis |
|---|---|
| `DATABASE_URL` | z bazy PostgreSQL w Railway |
| `SECRET_KEY` | dowolny długi losowy ciąg |
| `CLICKUP_API_TOKEN` | token API ClickUp (avatar → Settings → Apps → API Token, zaczyna się od `pk_`) |
| `CLICKUP_LIST_ID` | ID listy ClickUp z leadami (liczba po `/li/` w adresie listy) |
| `CLICKUP_POLL_SECONDS` | opcjonalnie, co ile sekund sprawdzać ClickUp (domyślnie 300) |
| `CALC_DAY_PRICE` | opcjonalnie, stawka za dzień 8 h w € netto (domyślnie 2500) |
| `CALC_MAX_DAY` | opcjonalnie, maks. cena za dzień 8 h z dojazdem i noclegiem (domyślnie 3400) |
| `CALC_KM_PRICE` | opcjonalnie, € za km od granicy (domyślnie 1.0) |
| `CALC_LODGING` | opcjonalnie, € za nocleg ekipy za noc (domyślnie 150) |
| `CALC_ROUND_TO` | opcjonalnie, zaokrąglenie ceny w górę do (domyślnie 100) |

Tabele i nowe kolumny tworzą się same przy starcie.

## Import z ClickUp

- Nazwa taska → firma. Pola formularza rozpoznawane po nazwie (niemieckie i polskie),
  np. „Telefon”, „E-Mail”, „Ansprechpartner”, „Ort”, „Datum”; drugie pole e-mail → „Zweite E-Mail”.
- Pola, których nie udało się dopasować, trafiają do notatki leada.
- Raz zaimportowanego leada import nie nadpisuje, tylko uzupełnia puste pola.
- Lead usunięty w CRM nie wraca przy kolejnym imporcie.
- Osoba z zakładki Team z tym samym e-mailem co w ClickUp dostaje przypisane do niej taski automatycznie.

## Lokalnie

```
pip install -r requirements.txt
python app.py   # http://localhost:5000, baza SQLite crm-de.db
```
