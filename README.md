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

## Installazione

Serve Python 3.10 o superiore (testato con 3.14).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# solo se userai browser: true
playwright install chromium
```

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
| `urls` | `[]` | elenco di URL (lo schema `https://` è opzionale) |
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
| `shots` | `screenshots` | cartella degli screenshot |
| `html` / `json` | – | percorso dei report |
| `verbose` | `0` | log su stderr: `1` = passi principali, `2` = dettaglio |
| `log_file` | – | log completo (livello DEBUG) su file |
| `ignore_js_errors` | vedi sotto | regex degli errori JS da ignorare |

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
- **Errori JS ignorati.** Alcuni errori sono rumore noto, per esempio `requestStorageAccess: Permission denied`, lanciato da widget di terze parti in Chromium headless e non visibile a un utente reale. Quelli che corrispondono a `ignore_js_errors` non entrano nel report ma restano nel log a `verbose: 2`. Per ignorarne altri basta aggiungere una regex alla lista.
- **Log.** Con `verbose: 2` o `log_file` si vede cosa succede passo per passo: richieste, redirect e tempi, ogni problema rilevato e ogni errore ignorato con il pattern che lo ha scartato. In caso di fallimento del browser viene registrato il traceback completo.
- **Concorrenza.** Un valore alto di `concurrency` può generare falsi FAIL (`ReadError`, connessioni rifiutate) su server fragili: in caso di errori di rete sporadici prova ad abbassarlo.

## Limiti noti

- Non c'è nessun retry: un errore di rete transitorio produce subito un FAIL.
- Gli errori TLS di validazione (certificato scaduto, host non corrispondente, self-signed) vengono segnalati come "non raggiungibile" con un messaggio generico, perché la connessione fallisce prima del controllo dedicato al certificato.
- Per i contenuti non HTML (per esempio JSON) il browser segnala anche "pagina vuota", oltre all'avviso sul content-type.
