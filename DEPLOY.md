# Deploy: Streamlit Community Cloud + Turso

## 1. GitHub — stwórz repo

Na github.com kliknij **New repository** → nazwa np. `leki-tymka` → **Private** → **Create**.
Następnie z katalogu projektu (`/Users/bartekp/Projekty/lekiTymka`) w terminalu:

```bash
git remote add origin https://github.com/<twoj-login>/leki-tymka.git
git branch -M main
git push -u origin main
```

## 2. Turso — baza w chmurze

1. https://turso.tech → **Sign up** → „Continue with GitHub".
2. Po zalogowaniu **Create database** → nazwa np. `leki-tymka` → region najbliżej (Frankfurt / Warsaw).
3. Na stronie bazy zobaczysz:
   - **Database URL** — coś w stylu `libsql://leki-tymka-xxxxx.turso.io`
   - **Create Token** (klik) → skopiuj token, pokaże się raz.

Te dwie wartości zaraz wkleimy w Streamlit.

## 3. Streamlit Community Cloud — deploy

1. https://share.streamlit.io → **Continue with GitHub** → autoryzacja.
2. **New app** → wybierz repo `leki-tymka`, branch `main`, main file `app.py` → **Deploy**.
3. Po pierwszym buildzie (1–2 min) wejdź w **⋯ → Settings → Secrets** i wklej:

   ```
   TURSO_DATABASE_URL = "libsql://leki-tymka-xxxxx.turso.io"
   TURSO_AUTH_TOKEN = "eyJhbG..."
   ```

   Save → apka sama się zrestartuje z podpiętą bazą.

Pierwsze wejście zainicjalizuje schema i zrobi seed (6 leków + schemat 08:00/20:00 + po 2 puste opakowania). **Pamiętaj skorygować stan opakowań w zakładce Apteczka** jeśli któreś są już napoczęte.

## 4. (Opcjonalnie) Przeniesienie lokalnego `leki.db` do Turso

Jeśli używałeś apki lokalnie i masz już historię w `leki.db`, którą chcesz zachować:

```bash
# instalacja CLI Turso
curl -sSfL https://get.tur.so/install.sh | bash

# login
turso auth login

# import lokalnej bazy do istniejącej Turso DB (OSTROŻNIE — nadpisuje zawartość)
turso db shell leki-tymka < <(sqlite3 leki.db .dump)
```

Lub prościej: najpierw zdeployuj pustą (kroki 1–3), zobacz że działa, potem wchodząc w apkę ręcznie skoryguj stany opakowań w zakładce Apteczka.

## Koszty

- GitHub (publiczne lub prywatne repo jednoosobowe): **0 zł**
- Streamlit Community Cloud: **0 zł** (usypia po ~10 min bezczynności; pierwsze wejście po przerwie = 20–40 s bootu)
- Turso free tier: **0 zł** (9 GB, 1 mld wierszy, zdecydowanie wystarczy)

## Uwaga: efemeryczny dysk Streamlit

Plik `leki.db` w kontenerze Streamlit Cloud jest embedded replica Turso — piszesz lokalnie, po każdym commicie dane synchronizują się do Turso. Jeśli kontener się zrestartuje, apka pobiera świeżą kopię z Turso. Historia bezpieczna.
