# AGENTS.md

Guida per chi lavora sul progetto, che sia un agente AI o una persona nuova nel team. Leggila a inizio sessione e **aggiornala a fine sessione** (vedi "Come mantenere questo file").

Per utenti finali e dettagli di configurazione vedi [README.md](README.md).

## Cos'è
`py-websitechecker` verifica la salute di N siti web dal punto di vista del cliente (HTTP + opzionalmente browser reale). Tutto il codice è in un unico file, [sitechecker.py](sitechecker.py). Le opzioni si danno solo tramite file YAML.

## Struttura del repository

| File | Ruolo |
|---|---|
| [sitechecker.py](sitechecker.py) | Tutto il tool: controlli HTTP, controlli browser, report, caricamento config, logging |
| [config.yaml](config.yaml) | Configurazione di default, versionata e commentata |
| `.config.local.yaml` | Configurazione personale, **ignorata da git**: non va mai committata |
| [requirements.txt](requirements.txt) | Dipendenze dirette (`httpx`, `PyYAML`, `playwright`) |
| [README.md](README.md) | Documentazione per l'utente |

Non ci sono ancora test automatici né una cartella `tests/`.

## Comandi

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium            # solo per browser: true

python sitechecker.py                  # usa ./config.yaml
python sitechecker.py -c .config.local.yaml
```

Python 3.10 o superiore (il venv del progetto usa 3.14). Il Python di sistema su macOS è 3.9 e non va usato.

## Architettura di `sitechecker.py`

- `DEFAULTS` è l'**unica fonte di verità** delle opzioni e dei loro default. `config.yaml` e la tabella del README devono restare allineati a questo dizionario.
- `load_config()` legge il YAML, rifiuta le chiavi sconosciute e restituisce un `argparse.Namespace`: il resto del codice legge quindi `args.<opzione>`. L'unico argomento da riga di comando è `-c/--config`.
- `check_http()` fa i controlli di livello 1 per un sito (redirect, TLS, contenuto, risorse, link interni). `SOFT_ERRORS` è la lista di pattern per le pagine di errore con status 200.
- `check_browser()` fa i controlli di livello 2 con Playwright (desktop e mobile, screenshot). Gli errori JS che corrispondono a `ignore_js_errors` vengono scartati e loggati a livello DEBUG.
- `Result.add(sev, msg)` registra un problema (`FAIL`/`WARN`) e lo scrive nel log. Il verdetto del sito è derivato dai problemi: FAIL se ce n'è almeno uno grave, altrimenti WARN, altrimenti OK.
- Output: `print_table`, `write_html`, e il JSON scritto in `main_async`. L'exit code è `1` solo se c'è almeno un FAIL.
- Logging: logger `sitecheck`, configurato da `setup_logging` in base a `verbose` e `log_file`.

## Convenzioni

- Messaggi, log e commenti sono **in italiano**; mantieni la lingua.
- Un'opzione nuova va aggiunta in `DEFAULTS`, in `config.yaml` e nella tabella del README, nello stesso intervento.
- Le opzioni non si aggiungono come parametri da riga di comando: è una scelta esplicita del progetto.
- `from __future__ import annotations` è in testa al file; non rimuoverlo.
- Per i nuovi controlli decidi la severità con questo criterio: FAIL se il visitatore non riesce a usare il sito, WARN se è degradato o sospetto.
- Prima di dichiarare un controllo "funzionante", provalo su un caso reale o simulato che lo faccia scattare, non solo su un sito sano.

## Cose da sapere (insidie già incontrate)

- Un valore alto di `concurrency` produce falsi FAIL (`ReadError`, connessioni rifiutate) su server fragili, come un semplice `http.server` locale. Prima di dare la colpa allo script, riprova con una concorrenza bassa.
- `requestStorageAccess: Permission denied` è rumore di widget di terze parti in Chromium headless, non un difetto del sito. È già in `ignore_js_errors`.
- In zsh una variabile con più URL non viene divisa in argomenti: usa `urls` o `urls_file` nel config.
- Ogni sessione di test con `browser: true` crea una cartella di screenshot (default `screenshots/`). Non è nel `.gitignore`: non committarla.

## Stato e lavoro aperto

Difetti noti, già verificati con test su un server locale difettoso e su host `badssl.com`, non ancora corretti:

1. **Errori TLS diagnosticati male.** Certificato scaduto, host non corrispondente, self-signed e catena non attendibile vengono riportati come `non raggiungibile (...)`, a volte con messaggio vuoto, perché httpx fallisce prima che parta `tls_days_left`. Da distinguere nel messaggio.
2. **Nessun retry** sugli errori di connessione e lettura transitori: un singolo errore produce un FAIL. Proposta: 1-2 retry con breve attesa, solo per quegli errori (non per timeout o errori HTTP).
3. Rumore minore: per i contenuti non HTML il browser segnala anche "pagina vuota"; su una 404 compare un doppione "risorsa 404"; "nessun link interno trovato" appare su ogni pagina senza link.

Idee non avviate:
- Aggiungere una cartella `tests/` con un server di prova che simula i difetti (pagina vuota, stack trace, risorse rotte, errori JS, redirect in loop, timeout, 404/500) e verifica i verdetti attesi.
- Aggiungere `screenshots/` al `.gitignore`.

## Come mantenere questo file

Questo file è la memoria condivisa del progetto: serve a non perdere contesto tra una sessione e l'altra.

- **A inizio sessione**: leggi questo file e il README, poi `git log --oneline -10` e `git status` per vedere cosa è cambiato.
- **A fine sessione**, aggiorna:
  - "Stato e lavoro aperto": togli ciò che è stato risolto, aggiungi ciò che è emerso e non è stato chiuso.
  - "Cose da sapere": aggiungi ogni insidia che ti ha fatto perdere tempo.
  - "Architettura" e "Convenzioni" se sono cambiate.
- Scrivi solo ciò che **non si ricava dal codice o dalla cronologia git**: decisioni e il loro perché, insidie, lavoro in sospeso. Non duplicare il contenuto del codice.
- Non inserire segreti, credenziali o URL interni di clienti: per questi usa `.config.local.yaml`.
