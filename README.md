# Posevo CRM

Prosty CRM dla Posevo do obsługi niemieckich leadów. Interfejs jest po niemiecku.

- Leady wpadają automatycznie z listy ClickUp (co 5 minut, plus przycisk „ClickUp abrufen”).
- Dwa statusy: **Nicht unterschrieben** (każdy nowy lead) i **Unterschrieben**.
- Osoba odpowiedzialna, dane kontaktowe, termin, notatki i komentarze przy każdym leadzie.
- Bez logowania i haseł: każdy wybiera siebie z listy „Ich bin” (zapisywane w cookie).
  Zespół dodaje się w zakładce **Team**.
- Bez funkcji AI i bez połączenia z API 33bots.

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
