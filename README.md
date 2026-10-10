# py-websitechecker

Verifica rapida della salute di uno o più siti web **dal punto di vista del cliente**: risponde alla domanda "il sito che vede un visitatore funziona davvero?", non solo "il server risponde?".

Il tool è uno script singolo ([sitechecker.py](sitechecker.py)), configurato tramite un file YAML. Produce una tabella a terminale e, se richiesto, un report HTML e/o JSON. L'exit code (`0` = tutto OK/WARN, `1` = almeno un FAIL) lo rende utilizzabile in cron e CI.

## Cosa controlla

**Livello 1 – solo HTTP (default, veloce)**
- raggiungibilità, redirect (anche catene lunghe o cicli), status finale, tempi di risposta
- scadenza e validità del certificato TLS
- redirect finale che scende da HTTPS a HTTP
- pagina non vuota, titolo presente, content-type HTML
- pagine di errore "soft" con status 200: stack trace Java, errore Tomcat, errori proxy/gateway (502/503/504), setup wizard OpenCms esposto, directory listing, errore applicativo, home che sembra una 404, testo placeholder (lorem ipsum), JSP/EL non interpretata
- risorse (img, css, js) rotte e mixed content
- campione di link interni rotti

**Livello 2 – browser reale (`browser: true`, richiede Playwright)**
- carica ogni pagina in Chromium, sia in versione desktop (1366×768) sia mobile (390×844)
- errori JavaScript in console, richieste fallite, risorse 4xx/5xx
- immagini non renderizzate, overflow orizzontale su mobile, pagina visivamente vuota
- salva uno screenshot per ogni viewport

**Esplorazione e giudizio sulle pagine (con `browser: true`)**
- oltre alla pagina indicata visita fino a `explore_pages` pagine interne, scelte tra **sezioni diverse** del sito (si preferiscono i link di navigazione; si saltano PDF, download e logout)
- stile e layout (desktop e mobile): CSS dichiarato ma non caricato (pagina "spaginata"), pagina senza alcun CSS, testo con contrasto illeggibile, elemento fisso che copre la pagina (banner cookie o modale), overflow orizzontale con indicazione dell'elemento responsabile, meta viewport assente
- contenuto (desktop): titolo mancante o generico, titolo o `h1` che indicano un errore ("404", "not found", "accesso negato"…), `h1` assente, poco contenuto, pagina fatta quasi solo di link, pagina di login al posto di un contenuto
- sulle pagine esplorate anche HTTP ≥ 400 e le pagine di errore "soft" (stack trace, Tomcat, gateway…)

## Installazione

Serve Python 3.10 o superiore (testato con 3.14).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# solo se userai browser: true
playwright install chromium
```

Su Windows (PowerShell) i comandi cambiano: `python3` e `source` non esistono.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium   # solo se userai browser: true
```

Se PowerShell blocca lo script di attivazione, esegui una volta
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

Su macOS, se il Python di sistema è troppo vecchio: `brew install python@3.14`.

## Utilizzo

Da riga di comando si indica solo il file di configurazione; tutte le opzioni stanno nel file YAML.

```bash
python sitechecker.py                         # usa ./config.yaml
python sitechecker.py -c .config.local.yaml   # usa un file locale
```

### Configurazione

[config.yaml](config.yaml) contiene i valori di default, commentati. Per un uso personale copialo in `.config.local.yaml`, che è ignorato da git, e modifica solo le chiavi che ti servono: quelle omesse assumono il default.

| Chiave | Default | Significato |
|---|---|---|
| `urls` | `[]` | URL (lo schema `https://` è opzionale) oppure mappe con controlli per sito: `url`, `expect_host`, `expect_text`, `forbid_text` (vedi [config.yaml](config.yaml)) |
| `retries` | `2` | ritentativi sugli errori di rete transitori (non su timeout ed errori HTTP) |
| `retry_wait` | `1.0` | secondi di attesa tra i tentativi |
| `samples` | `3` | misure del tempo di risposta: il valore usato è la mediana |
| `urls_file` | – | file con un URL per riga (`#` per i commenti), in aggiunta a `urls` |
| `browser` | `false` | abilita il check con browser reale |
| `links` | `10` | link interni da campionare (`0` = nessuno) |
| `max_resources` | `40` | risorse da verificare per pagina |
| `timeout` | `15` | timeout in secondi |
| `slow_ms` | `3000` | oltre questa soglia la pagina è "lenta" (WARN) |
| `tls_warn_days` | `21` | WARN se il certificato scade entro N giorni |
| `insecure` | `false` | ignora gli errori TLS (sconsigliato) |
| `concurrency` | `10` | siti controllati in parallelo |
| `browser_concurrency` | `3` | pagine aperte in parallelo nel browser |
| `explore_pages` | `3` | pagine interne da visitare nel browser oltre alla prima (`0` = solo la prima) |
| `explore_exclude` | logout, PDF e altri file | regex dei link da non visitare |
| `min_words` | `40` | sotto questa soglia la pagina è "poco informativa" (WARN) |
| `shots` | `screenshots` | cartella degli screenshot |
| `html` / `json` | – | percorso dei report |
| `verbose` | `0` | log su stderr: `1` = passi principali, `2` = dettaglio |
| `log_file` | – | log completo (livello DEBUG) su file |
| `ignore_js_errors` | vedi sotto | regex degli errori JS da ignorare |
| `ignore_failed_requests` | analytics | regex delle richieste fallite del browser da ignorare (beacon di Google Analytics, GTM, DoubleClick, Facebook) |
| `interval` | `0` | secondi tra un giro e l'altro: se > 0 il tool resta in esecuzione e ripete i controlli fino a Ctrl+C (`0` = una sola esecuzione) |
| `history` | – | file JSONL con lo storico: abilita il confronto con l'esecuzione precedente |
| `history_keep` | `30` | esecuzioni conservate nello storico |
| `confirm_wait` | `0` | secondi di attesa prima di ricontrollare i siti in FAIL (`0` = nessun ricontrollo) |
| `notify_webhook` | – | webhook (Slack/Teams/compatibile) a cui inviare l'avviso |
| `notify_on` | `change` | `change`: solo cambi di stato che coinvolgono un FAIL (richiede `history`); `fail`: a ogni FAIL |
| `heartbeat_url` | – | URL chiamato a fine esecuzione, per accorgersi che il monitor ha smesso di girare |

Il file viene validato all'avvio: una chiave sconosciuta (per esempio un errore di battitura) blocca l'esecuzione con un messaggio che elenca le chiavi valide.

Esempio di `.config.local.yaml`:

```yaml
urls:
  - https://www.example.com
  - https://shop.example.com
browser: true
html: report.html
json: report.json
verbose: 1
log_file: sitecheck.log
```

## Note sulla logica di funzionamento

- **Esiti.** Ogni sito riceve un verdetto: `FAIL` se ha almeno un problema grave, altrimenti `WARN` se ha almeno un avviso, altrimenti `OK`. Il codice di uscita è `1` solo in presenza di almeno un `FAIL`.
- **Cosa è FAIL e cosa WARN.** Sono FAIL: sito non raggiungibile, timeout, HTTP ≥ 400, certificato scaduto o non valido, redirect che scende a HTTP, pagina vuota, stack trace o pagine di errore visibili, CSS/JS rotti, link interni rotti. Sono WARN: lentezza, titolo mancante, immagini rotte, mixed content, errori JS, overflow su mobile, testo placeholder.
- **Testo visibile.** I controlli sulle pagine di errore "soft" lavorano sul testo realmente visibile: script, stili e `noscript` vengono scartati prima dell'analisi.
- **Browser.** Le pagine con status assente o ≥ 500 non vengono aperte nel browser. Il tool aspetta il caricamento e poi, per al più 4 secondi, che la rete si quieti.
- **Esplorazione.** I link candidati vengono letti dalla prima pagina (desktop), filtrati (stesso dominio, niente file, niente `explore_exclude`) e scelti a rotazione tra sezioni diverse, usando come sezione il primo segmento del percorso (un prefisso lingua come `/en/` viene saltato). Ogni pagina esplorata è aperta sia su desktop sia su mobile. Gli avvisi sulle pagine esplorate riportano il percorso, per esempio `[desktop] /prodotti/: …`.
- **Euristiche, non verità assolute.** I controlli su stile e contenuto sono segnali: un WARN dice "controlla questa pagina", non che sia rotta. Il contrasto non viene valutato dove c'è un'immagine di sfondo, e i controlli di contenuto girano solo su desktop perché su mobile sono identici.
- **Errori JS ignorati.** Alcuni errori sono rumore noto, per esempio `requestStorageAccess: Permission denied`, lanciato da widget di terze parti in Chromium headless e non visibile a un utente reale. Quelli che corrispondono a `ignore_js_errors` non entrano nel report ma restano nel log a `verbose: 2`. Per ignorarne altri basta aggiungere una regex alla lista.
- **Log.** Con `verbose: 2` o `log_file` si vede cosa succede passo per passo: richieste, redirect e tempi, ogni problema rilevato e ogni errore ignorato con il pattern che lo ha scartato. In caso di fallimento del browser viene registrato il traceback completo.
- **Concorrenza.** Un valore alto di `concurrency` può generare falsi FAIL (`ReadError`, connessioni rifiutate) su server fragili: in caso di errori di rete sporadici prova ad abbassarlo.

## Uso come monitor periodico

Per far girare il tool a intervalli si può usare `interval: 300` (resta in esecuzione e ripete i controlli ogni 5 minuti fino a Ctrl+C) oppure lo scheduler del sistema (Utilità di pianificazione di Windows, cron, GitHub Actions) con `interval: 0`. In ogni caso conviene:

```yaml
history: history.jsonl      # confronto con l'esecuzione precedente
confirm_wait: 30            # un FAIL va confermato dopo 30 s prima di essere considerato tale
notify_webhook: https://hooks.slack.com/...   # avviso solo sui cambi di stato con un FAIL
heartbeat_url: https://hc-ping.com/...        # un servizio esterno avvisa se il monitor non gira più
```

- Con `interval` l'intervallo è contato dall'inizio di ciascun giro; il file di configurazione viene riletto a ogni giro (se la modifica è sbagliata si continua con l'ultima versione valida, con un errore nel log) e un giro fallito non ferma il monitor. Gli screenshot di un giro sovrascrivono quelli del precedente. L'exit code non ha significato in questa modalità: usa avvisi e storico.
- Con `history` ogni esecuzione aggiunge una riga al file; il tool stampa i siti che sono peggiorati o migliorati e quelli il cui tempo supera di oltre il doppio (e di almeno 500 ms) la mediana storica, quando ci sono almeno 3 esecuzioni precedenti.
- L'avviso con `notify_on: change` parte quando un sito passa a FAIL o esce da FAIL, quindi un sito rimasto giù non viene ripetuto a ogni esecuzione.
- Il webhook riceve `{"text": "..."}`, compatibile con Slack e con i webhook entranti generici.

## Test

```bash
python -m unittest discover -s tests -v
```

Coprono le funzioni pure (`review_metrics`, `pick_links`, diagnosi TLS, storico) e i controlli HTTP contro un server locale che simula i difetti.

## Limiti noti

- `expect_text` e `forbid_text` guardano la sola pagina principale (con `browser: true` il testo renderizzato, altrimenti il testo visibile dell'HTML).
- Per i contenuti non HTML (per esempio JSON) il browser segnala anche "pagina vuota", oltre all'avviso sul content-type.
- Non c'è ancora una verifica dei link con segnaposto non risolti (per esempio `???label.xyz???`), che oggi emergono solo se si finisce per visitare quella pagina.
