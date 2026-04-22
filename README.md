# Leki Tymka

Prosta apka w Streamlit do pilnowania dawek i zapasu leków Tymka. Zaplanowane dawki (08:00 / 20:00) odliczają się same, doraźne (np. dodatkowy Ventolin) dodajesz jednym kliknięciem, wykupione opakowania zasilają apteczkę.

## Uruchomienie lokalne

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/streamlit run app.py
```

Apka otworzy się na `http://localhost:8501`. Bez żadnych dodatkowych sekretów używa lokalnego pliku `leki.db` (SQLite).

## Struktura

| Plik | Co |
|---|---|
| `app.py` | Cała apka: UI, logika, schema, seed |
| `requirements.txt` | Zależności |
| `.streamlit/secrets.toml.example` | Szablon sekretów (Turso URL, token, hasło) |
| `DEPLOY.md` | Instrukcja deploymentu krok po kroku |

## Jak to działa

- **Leki** — lista z receptami (Ventolin, Momester, Montelukast, Flixotide, Seretide, Clatra). Edytowalne w kodzie (`SEED_MEDS`).
- **Schemat** — plan przyjmowania (kto, o której, ile dawek). Przy zmianie dawkowania zamykasz stary wpis (`active_to`) i dodajesz nowy — historia zachowana.
- **Opakowania** — FIFO: każda dawka odejmuje z najstarszego aktywnego opakowania. Gdy jedno się skończy (`doses_left = 0`), automatycznie przechodzi na następne.
- **Domknięcie slotów** — przy każdym otwarciu apki sprawdzamy które zaplanowane dawki z przeszłości nie są zarejestrowane, i dopisujemy je jako przyjęte automatycznie (`kind=scheduled, auto=1`). Jeśli Tymek danej dawki *nie* wziął — kliknij „pomiń" (przywraca dawkę do opakowania i oznacza jako `skipped`).
- **Doraźnie** — zakładka „Dziś" → „Dawka doraźna": lek, liczba dawek, timestamp.

## Zakładki

- **Dziś** — dzisiejsze sloty + doraźne.
- **Apteczka** — stan ogólny, ostrzeżenie < 7 dni, szczegóły opakowań, dodawanie wykupionych.
- **Schemat** — aktywne + historyczne wpisy, edycja.
- **Historia** — 100 ostatnich przyjęć (ręczne vs auto).

## Deploy

Zobacz [DEPLOY.md](DEPLOY.md) — GitHub + Turso + Streamlit Cloud, ~15 min klikania.

## Dane

- Lokalnie: plik `leki.db` w katalogu projektu (w `.gitignore`, nie idzie do repo).
- W chmurze: Turso DB (sync'owana przy każdym commicie do bazy). Embedded replica w kontenerze Streamlit.
- **Recepty PDF**: w `.gitignore` (`*.pdf`) — dane medyczne nie trafiają do repo.
- Apka na Streamlit Cloud jest publicznie dostępna po URL-u, ale zabezpieczona hasłem (secret `APP_PASSWORD`).
