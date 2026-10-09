#!/usr/bin/env python3
"""
sitecheck - verifica rapida della salute di N siti dal punto di vista del cliente.

Livello 1 (default, veloce, solo HTTP):
  raggiungibilita', redirect, status finale, tempi, scadenza certificato TLS,
  pagina non vuota, titolo presente, pagine di errore "soft" (stack trace, Tomcat,
  gateway, setup wizard OpenCms, directory listing, placeholder),
  risorse (img/css/js) rotte, mixed content, campione di link interni.

Livello 2 (browser: true, richiede Playwright):
  carica la pagina in un browser vero (desktop + mobile) e rileva errori JS in console,
  richieste fallite, risorse 4xx/5xx, immagini rotte, overflow orizzontale su mobile,
  pagina visivamente vuota. Salva screenshot.

Uso (tutte le opzioni stanno nel file di configurazione YAML, vedi config.yaml):
  python sitechecker.py                           # usa ./config.yaml
  python sitechecker.py -c .config.local.yaml     # file locale non versionato

Exit code: 0 = tutto OK/WARN, 1 = almeno un FAIL (utile in cron/CI).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import socket
import ssl
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import yaml

UA = "Mozilla/5.0 (compatible; sitecheck/1.0)"

log = logging.getLogger("sitecheck")

# valori di default; ogni chiave puo' essere sovrascritta dal file di configurazione
DEFAULTS = {
    "urls": [],               # elenco di URL (lo schema https:// e' opzionale)
    "urls_file": None,        # file con un URL per riga (# per commenti), in aggiunta a `urls`
    "browser": False,         # check con browser reale (Playwright)
    "links": 10,              # link interni da campionare (0 = nessuno)
    "max_resources": 40,
    "timeout": 15,
    "slow_ms": 3000,
    "tls_warn_days": 21,
    "concurrency": 10,
    "browser_concurrency": 3,
    "shots": "screenshots",
    "html": None,             # percorso del report HTML
    "json": None,             # percorso del report JSON
    "insecure": False,        # ignora errori TLS (sconsigliato)
    "verbose": 0,             # 0 = silenzioso, 1 = passi principali, 2 = dettaglio
    "log_file": None,         # log completo (DEBUG) su file
    # errori JS del browser noti come innocui (rumore di terze parti / limiti dell'headless); regex
    "ignore_js_errors": [r"^Failed to load resource", r"requestStorageAccess"],
}

# (regex, severita', messaggio) applicati al testo VISIBILE della pagina
SOFT_ERRORS = [
    (r"java\.lang\.\w+(Exception|Error)|\bat (org|java|com)\.[\w.$]+\(", "FAIL", "stack trace Java visibile"),
    (r"HTTP Status [45]\d\d", "FAIL", "pagina di errore Tomcat"),
    (r"\b(502 Bad Gateway|503 Service (Temporarily )?Unavailable|504 Gateway Time-?out)\b", "FAIL", "errore proxy/gateway"),
    (r"OpenCms Setup Wizard|Alkacon OpenCms Setup", "FAIL", "setup wizard OpenCms esposto"),
    (r"^\s*Index of /", "FAIL", "directory listing"),
    (r"Internal Server Error|Application Error", "FAIL", "errore applicativo"),
    (r"\b404\b.{0,40}(not found|non trovata)|page not found|pagina non trovata", "WARN", "la home sembra una 404"),
    (r"lorem ipsum", "WARN", "testo placeholder (lorem ipsum)"),
    (r"<%|<jsp:|\$\{(param|requestScope|cms)\.", "WARN", "JSP/EL non interpretata"),
]


@dataclass
class Result:
    url: str
    final_url: str = ""
    status: int = 0
    ms: int = 0
    tls_days: int | None = None
    title: str = ""
    issues: list = field(default_factory=list)  # (sev, msg)
    screenshots: list = field(default_factory=list)

    def add(self, sev, msg):
        log.debug("%s %s: %s", sev, self.url, msg)
        self.issues.append((sev, msg))

    @property
    def verdict(self):
        sevs = {s for s, _ in self.issues}
        return "FAIL" if "FAIL" in sevs else "WARN" if "WARN" in sevs else "OK"


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.links, self.resources = [], []
        self.text = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag in ("img", "script", "source", "iframe") and a.get("src"):
            self.resources.append((tag, a["src"]))
        elif tag == "link" and a.get("href") and "stylesheet" in (a.get("rel") or ""):
            self.resources.append(("css", a["href"]))
        if tag == "script" and a.get("src"):
            pass

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip and data.strip():
            self.text.append(data.strip())


def tls_days_left(host, port=443, timeout=5):
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as s:
            with ctx.wrap_socket(s, server_hostname=host) as ss:
                exp = ss.getpeercert()["notAfter"]
        end = datetime.fromtimestamp(ssl.cert_time_to_seconds(exp), tz=timezone.utc)
        return (end - datetime.now(timezone.utc)).days
    except ssl.SSLCertVerificationError as e:
        return f"INVALID: {e.verify_message}"
    except Exception:
        return None


async def probe(client, url, method="HEAD"):
    try:
        r = await client.request(method, url)
        if method == "HEAD" and r.status_code in (403, 405, 501):
            r = await client.get(url)
        return r.status_code
    except Exception as e:
        return type(e).__name__


async def check_http(client, url, args, sem):
    res = Result(url=url)
    async with sem:
        t0 = time.monotonic()
        log.info("HTTP GET %s", url)
        try:
            r = await client.get(url)
        except httpx.ConnectError as e:
            res.add("FAIL", f"non raggiungibile ({e})")
            return res
        except httpx.TimeoutException:
            res.add("FAIL", f"timeout oltre {args.timeout}s")
            return res
        except Exception as e:
            res.add("FAIL", f"errore {type(e).__name__}: {e}")
            return res
        res.ms = int((time.monotonic() - t0) * 1000)
        res.status = r.status_code
        res.final_url = str(r.url)
        log.debug("%s -> %s status=%s ms=%s redirect=%d", url, res.final_url, res.status, res.ms, len(r.history))

        if len(r.history) > 5:
            res.add("WARN", f"{len(r.history)} redirect consecutivi")
        if r.status_code >= 400:
            res.add("FAIL", f"HTTP {r.status_code}")
        if res.ms > args.slow_ms:
            res.add("WARN", f"lento: {res.ms} ms")
        if url.startswith("https") and not str(r.url).startswith("https"):
            res.add("FAIL", "il redirect finale scende a HTTP")

        host = urlparse(str(r.url)).hostname
        if str(r.url).startswith("https") and host:
            d = await asyncio.to_thread(tls_days_left, host)
            res.tls_days = d
            if isinstance(d, str):
                res.add("FAIL", f"certificato {d}")
            elif d is not None and d < 0:
                res.add("FAIL", "certificato scaduto")
            elif d is not None and d < args.tls_warn_days:
                res.add("WARN", f"certificato scade fra {d} giorni")

        ctype = r.headers.get("content-type", "")
        if "html" not in ctype:
            res.add("WARN", f"content-type inatteso: {ctype or 'assente'}")
            return res

        p = PageParser()
        try:
            p.feed(r.text)
        except Exception:
            res.add("WARN", "HTML non interpretabile")
        res.title = " ".join(p.title.split())[:120]
        visible = " ".join(p.text)

        if len(visible) < 80:
            res.add("FAIL", "pagina praticamente vuota")
        if not res.title:
            res.add("WARN", "titolo mancante")
        for pat, sev, msg in SOFT_ERRORS:
            if re.search(pat, visible, re.I | re.M):
                res.add(sev, msg)

        # risorse
        base = str(r.url)
        seen, targets = set(), []
        for kind, src in p.resources:
            if src.startswith(("data:", "javascript:", "#")):
                continue
            absu = urljoin(base, src)
            if absu in seen:
                continue
            seen.add(absu)
            if base.startswith("https") and absu.startswith("http://"):
                res.add("WARN", f"mixed content: {absu[:90]}")
            targets.append((kind, absu))
        targets = targets[: args.max_resources]
        codes = await asyncio.gather(*(probe(client, u) for _, u in targets))
        for (kind, u), c in zip(targets, codes):
            if not (isinstance(c, int) and c < 400):
                res.add("FAIL" if kind in ("css", "script") else "WARN", f"{kind} rotto ({c}): {u[:90]}")

        # campione link interni
        host0 = urlparse(base).netloc
        links, seen_l = [], set()
        for h in p.links:
            if h.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            absu = urljoin(base, h).split("#")[0]
            if urlparse(absu).netloc == host0 and absu not in seen_l and absu != base:
                seen_l.add(absu)
                links.append(absu)
        links = links[: args.links]
        if not links and args.links:
            res.add("WARN", "nessun link interno trovato")
        codes = await asyncio.gather(*(probe(client, u, "GET") for u in links))
        for u, c in zip(links, codes):
            if not (isinstance(c, int) and c < 400):
                res.add("FAIL", f"link interno rotto ({c}): {u[:90]}")
    return res


async def check_browser(results, args):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Playwright non installato: pip install playwright && playwright install chromium", file=sys.stderr)
        return
    out = Path(args.shots)
    out.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(args.browser_concurrency)
    viewports = {"desktop": {"width": 1366, "height": 768}, "mobile": {"width": 390, "height": 844}}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()

        async def one(res, vp_name, vp):
            if res.status == 0 or res.status >= 500:
                log.info("browser: salto %s [%s] (status %s)", res.url, vp_name, res.status)
                return
            async with sem:
                log.info("browser: carico %s [%s]", res.url, vp_name)
                ctx = await browser.new_context(viewport=vp, user_agent=UA, ignore_https_errors=False,
                                                is_mobile=(vp_name == "mobile"))
                page = await ctx.new_page()
                errs, failed, bad = [], [], []
                page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e: errs.append(str(e)))
                page.on("requestfailed", lambda rq: failed.append(f"{rq.url[:90]} ({rq.failure})"))
                page.on("response", lambda rp: bad.append(f"{rp.status} {rp.url[:90]}") if rp.status >= 400 else None)
                tag = f"[{vp_name}] "
                try:
                    await page.goto(res.final_url or res.url, wait_until="load", timeout=args.timeout * 1000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=4000)
                    except Exception:
                        pass
                    broken = await page.evaluate(
                        "Array.from(document.images).filter(i=>i.complete&&i.naturalWidth===0&&i.src).map(i=>i.src.slice(0,90))")
                    overflow = await page.evaluate(
                        "document.documentElement.scrollWidth - document.documentElement.clientWidth")
                    textlen = await page.evaluate("(document.body.innerText||'').trim().length")
                    # evita duplicati: il 404 e' gia' in `bad`, il console error generico e' rumore
                    kept = []
                    for e in errs:
                        pat = next((p for p in args.ignore_js_errors if re.search(p, e)), None)
                        if pat:
                            log.debug("%s%s errore JS ignorato (%s): %s", tag, res.url, pat, e[:150])
                        else:
                            kept.append(e)
                    errs = kept
                    bad_urls = {b.split(" ", 1)[1] for b in bad}
                    failed = [f for f in failed if f.split(" (")[0] not in bad_urls]
                    for e in errs[:3]:
                        res.add("WARN", f"{tag}errore JS: {e[:110]}")
                    for f in failed[:3]:
                        res.add("WARN", f"{tag}richiesta fallita: {f}")
                    for b in bad[:3]:
                        res.add("WARN", f"{tag}risorsa {b}")
                    for b in broken[:3]:
                        res.add("WARN", f"{tag}immagine non renderizzata: {b}")
                    if vp_name == "mobile" and overflow > 8:
                        res.add("WARN", f"{tag}overflow orizzontale di {overflow}px")
                    if textlen < 50:
                        res.add("FAIL", f"{tag}pagina vuota nel browser")
                    shot = out / f"{re.sub(r'[^a-z0-9]+', '_', (urlparse(res.url).netloc or res.url).lower())}_{vp_name}.png"
                    await page.screenshot(path=str(shot))
                    res.screenshots.append(str(shot))
                except Exception as e:
                    log.exception("browser: %s [%s] fallito", res.url, vp_name)
                    res.add("FAIL", f"{tag}caricamento fallito: {type(e).__name__}")
                finally:
                    await ctx.close()

        await asyncio.gather(*(one(r, n, v) for r in results for n, v in viewports.items()))
        await browser.close()


def print_table(results):
    col = {"OK": "\033[32m", "WARN": "\033[33m", "FAIL": "\033[31m"}
    tty = sys.stdout.isatty()
    for r in results:
        c, z = (col[r.verdict], "\033[0m") if tty else ("", "")
        tls = f" tls:{r.tls_days}g" if isinstance(r.tls_days, int) else ""
        print(f"{c}{r.verdict:4}{z} {r.status or '---':>3} {r.ms:>5}ms{tls}  {r.url}")
        for sev, msg in r.issues:
            print(f"        {sev:4} {msg}")
    n = {v: sum(1 for r in results if r.verdict == v) for v in ("OK", "WARN", "FAIL")}
    print(f"\nTotale {len(results)}: OK {n['OK']}  WARN {n['WARN']}  FAIL {n['FAIL']}")


def write_html(results, path):
    colors = {"OK": "#d4edda", "WARN": "#fff3cd", "FAIL": "#f8d7da"}
    rows = []
    for r in results:
        iss = "<br>".join(f"<b>{s}</b> {escape(m)}" for s, m in r.issues) or "-"
        shots = " ".join(f'<a href="{escape(s)}">{Path(s).stem.split("_")[-1]}</a>' for s in r.screenshots)
        rows.append(
            f'<tr style="background:{colors[r.verdict]}"><td>{r.verdict}</td>'
            f'<td><a href="{escape(r.url)}">{escape(r.url)}</a><br><small>{escape(r.title)}</small></td>'
            f"<td>{r.status}</td><td>{r.ms} ms</td><td>{r.tls_days if r.tls_days is not None else ''}</td>"
            f"<td>{iss}</td><td>{shots}</td></tr>")
    html = (f"<!doctype html><meta charset=utf-8><title>sitecheck</title>"
            f"<style>body{{font:14px sans-serif;margin:20px}}table{{border-collapse:collapse;width:100%}}"
            f"td,th{{border:1px solid #bbb;padding:6px;vertical-align:top}}</style>"
            f"<h2>sitecheck {datetime.now():%Y-%m-%d %H:%M}</h2><table>"
            f"<tr><th>Esito<th>Sito<th>HTTP<th>Tempo<th>TLS gg<th>Problemi<th>Screenshot</tr>{''.join(rows)}</table>")
    Path(path).write_text(html, encoding="utf-8")


def load_urls(args):
    urls = list(args.urls)
    if args.urls_file:
        for line in Path(args.urls_file).read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if line:
                urls.append(line)
    out = []
    for u in urls:
        if not re.match(r"^https?://", u):
            u = "https://" + u
        if u not in out:
            out.append(u)
    return out


async def main_async(args):
    urls = load_urls(args)
    if not urls:
        sys.exit("Nessun URL: indica `urls` o `urls_file` nel file di configurazione")
    sem = asyncio.Semaphore(args.concurrency)
    limits = httpx.Limits(max_connections=args.concurrency * 4)
    async with httpx.AsyncClient(follow_redirects=True, timeout=args.timeout, headers={"User-Agent": UA},
                                 limits=limits, verify=not args.insecure) as client:
        results = await asyncio.gather(*(check_http(client, u, args, sem) for u in urls))
    if args.browser:
        await check_browser(results, args)
    print_table(results)
    if args.html:
        write_html(results, args.html)
    if args.json:
        Path(args.json).write_text(json.dumps([{**asdict(r), "verdict": r.verdict} for r in results],
                                              indent=2, ensure_ascii=False), encoding="utf-8")
    return 1 if any(r.verdict == "FAIL" for r in results) else 0


def load_config(path):
    p = Path(path)
    if not p.is_file():
        sys.exit(f"File di configurazione non trovato: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        sys.exit(f"YAML non valido in {p}: {e}")
    if not isinstance(data, dict):
        sys.exit(f"{p}: il file deve contenere una mappa chiave: valore")
    unknown = sorted(set(data) - set(DEFAULTS))
    if unknown:
        sys.exit(f"{p}: chiavi sconosciute: {', '.join(unknown)} (valide: {', '.join(DEFAULTS)})")
    cfg = {**DEFAULTS, **data}
    for k in ("urls", "ignore_js_errors"):
        if not isinstance(cfg[k], list):
            sys.exit(f"{p}: `{k}` deve essere una lista")
    return argparse.Namespace(**cfg)


def main():
    ap = argparse.ArgumentParser(description="Verifica rapida salute siti (cliente-centrica)")
    ap.add_argument("-c", "--config", default="config.yaml",
                    help="file di configurazione YAML (default: config.yaml)")
    cli = ap.parse_args()
    args = load_config(cli.config)
    setup_logging(args)
    log.info("configurazione: %s", cli.config)
    sys.exit(asyncio.run(main_async(args)))


def setup_logging(args):
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    console = logging.StreamHandler(sys.stderr)
    console.setLevel([logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)])
    console.setFormatter(fmt)
    log.addHandler(console)
    if args.log_file:
        fh = logging.FileHandler(args.log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        log.addHandler(fh)


if __name__ == "__main__":
    main()