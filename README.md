# Posevo CRM

Prosty CRM dla Posevo do obsługi niemieckich leadów. Interfejs jest po niemiecku.

- Leady wpadają automatycznie z listy ClickUp (co 5 minut, plus przycisk „ClickUp abrufen”).
- Dwa statusy: **Nicht unterschrieben** (każdy nowy lead) i **Unterschrieben**.
- Osoba odpowiedzialna, dane kontaktowe, termin, notatki i komentarze przy każdym leadzie.
- Kalkulator ceny z dojazdem liczonym od granicy (opis niżej).
- Bez logowania i haseł: każdy wybiera siebie z listy „Ich bin” (zapisywane w cookie).
  Zespół dodaje się w zakładce **Team**.
- Bez funkcji AI i bez połączenia z API 33bots.

## Kalkulator

Zakładka **Kalkulator** (albo przycisk „Preis kalkulieren” na karcie leada):

- **Cena** = dni × stawka dzienna (domyślnie 2500 € netto) + dojazd + opcjonalny dopłata, zaokrąglone w górę (domyślnie do 50 €).
- **Dojazd** liczy się tylko po niemieckiej stronie: od przejścia granicznego do miejsca eventu i z powrotem, × stawka za km (domyślnie 1 €/km).
- Wyjazd z **Warszawy lub Katowic**. Dla każdego startu kalkulator sam wybiera przejście z najkrótszą całą trasą
  (Frankfurt (Oder), Görlitz, Forst, Pomellen albo Waidhaus przez Czechy) i pokazuje oba warianty.
  „Automatisch” liczy z miasta, z którego cała trasa jest krótsza; można też wybrać start ręcznie.
- Wynik to jedna kwota „netto inkl. Anfahrt” (+ cena za dzień przy kilku dniach) i przycisk do skopiowania tekstu oferty.
- „Im Lead speichern” zapisuje cenę i rozpisanie w leadzie i w komentarzach.
- Kilometry: OpenStreetMap (Nominatim do wyszukania miejsca, OSRM do trasy). Gdy serwis nie odpowiada,
  kalkulator szacuje (linia prosta × 1,25) i to zaznacza; można też wpisać km od granicy ręcznie.
- Stawki domyślne zmienia się w zakładce **Einstellungen**.

## Wdrożenie na Railway

1. Nowy projekt → Deploy from GitHub → to repo.
2. Dodaj bazę **PostgreSQL** i podłącz `DATABASE_URL` do serwisu.
3. Zmienne środowiskowe:

| Zmienna | Opis |
|---|---|
| `DATABASE_URL` | z bazy PostgreSQL w Railway |
| `SECRET_KEY` | dowolny długi losowy ciąg |
| `CLICKUP_API_TOKEN` | token API ClickUp (Settings → Apps → API Token) |
| `CLICKUP_LIST_ID` | ID listy ClickUp z leadami dla Posevo (liczba z adresu listy) |
| `CLICKUP_POLL_SECONDS` | opcjonalnie, co ile sekund sprawdzać ClickUp (domyślnie 300) |

Tabele tworzą się same przy starcie.

## Import z ClickUp

- Nazwa taska → firma. Pola formularza są rozpoznawane po nazwie, po niemiecku i po polsku,
  np. „Telefon”, „E-Mail”, „Ansprechpartner”, „Ort”, „Datum”.
- Pola, których nie udało się dopasować, trafiają do notatki leada, więc nic nie ginie.
- Raz zaimportowanego leada import już nie nadpisuje, tylko uzupełnia puste pola.
- Lead usunięty w CRM nie wraca przy kolejnym imporcie.
- Jeśli osoba z zespołu ma w zakładce Team ten sam e-mail co w ClickUp, przypisane do niej
  taski trafiają do niej automatycznie.

## Lokalnie

```
pip install -r requirements.txt
python app.py   # http://localhost:5000, baza SQLite posevo.db
```
