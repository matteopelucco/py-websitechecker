"""Test di sitecheck. Esecuzione: python -m unittest discover -s tests -v

Le funzioni pure si provano direttamente; i controlli HTTP girano contro un server locale che simula i difetti
(pagina vuota, stack trace, 500, CSS rotto, redirect in loop, connessioni che cadono, 429, lentezza).
"""
import argparse
import asyncio
import http.server
import ssl
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sitechecker as sc  # noqa: E402

TEXT = "contenuto di prova per superare la soglia minima di testo visibile " * 3


def page(body, head=""):
    return f"<html><head><title>Prova</title>{head}</head><body><h1>Prova</h1><p>{body}</p></body></html>"


class Handler(http.server.BaseHTTPRequestHandler):
    drops = 0  # connessioni da scartare su /drop (impostato dal test)

    def log_message(self, *a):
        pass

    def send(self, code, body, headers=()):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(b)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b)

    def do_GET(self):
        p = self.path
        if p == "/drop":
            if Handler.drops > 0:
                Handler.drops -= 1
                self.connection.close()
                return
            return self.send(200, page(TEXT))
        if p == "/500":
            return self.send(500, "errore")
        if p == "/empty":
            return self.send(200, "<html><head><title>t</title></head><body></body></html>")
        if p == "/java":
            return self.send(200, page(TEXT + " java.lang.NullPointerException at com.acme.Foo(Foo.java:10)"))
        if p == "/css":
            return self.send(200, page(TEXT, '<link rel="stylesheet" href="/missing.css">'))
        if p == "/links":
            return self.send(200, page(TEXT + '<a href="/rl">a</a><a href="/gone">b</a>'))
        if p == "/rl":
            return self.send(429, "slow down")
        if p in ("/gone", "/missing.css"):
            return self.send(404, "no")
        if p == "/loop":
            return self.send(302, "", headers=[("Location", "/loop")])
        if p == "/slow":
            time.sleep(0.4)
        return self.send(200, page(TEXT))

    do_HEAD = do_GET


def run_check(site, **over):
    args = argparse.Namespace(**{**sc.DEFAULTS, "retry_wait": 0.01, "samples": 1, "links": 5, **over})

    async def go():
        async with httpx.AsyncClient(follow_redirects=True, timeout=5) as c:
            return await sc.check_http(c, site, args, asyncio.Semaphore(4))
    return asyncio.run(go())


def msgs(res, sev):
    return [m for s, m in res.issues if s == sev]


class HttpChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def check(self, path, site=None, **over):
        return run_check(sc.parse_site({"url": self.base + path, **(site or {})}, "test"), **over)

    def test_healthy_page_has_no_fail(self):
        r = self.check("/ok")
        self.assertNotEqual(r.verdict, "FAIL", r.issues)
        self.assertEqual(r.status, 200)

    def test_http_500_is_fail(self):
        self.assertIn("HTTP 500", msgs(self.check("/500"), "FAIL"))

    def test_empty_page_is_fail(self):
        self.assertIn("pagina praticamente vuota", msgs(self.check("/empty"), "FAIL"))

    def test_java_stack_trace_is_fail(self):
        self.assertIn("stack trace Java visibile", msgs(self.check("/java"), "FAIL"))

    def test_broken_css_is_fail(self):
        self.assertTrue(any(m.startswith("css rotto (404)") for m in msgs(self.check("/css"), "FAIL")))

    def test_redirect_loop_is_fail(self):
        self.assertEqual(self.check("/loop").verdict, "FAIL")

    def test_unreachable_is_fail(self):
        r = run_check(sc.parse_site("http://127.0.0.1:1", "test"), retries=1)
        self.assertTrue(any("non raggiungibile" in m for m in msgs(r, "FAIL")), r.issues)

    def test_links_429_is_warn_and_404_is_fail(self):
        r = self.check("/links")
        self.assertTrue(any("429" in m for m in msgs(r, "WARN")), r.issues)
        self.assertTrue(any(m.startswith("link interno rotto (404)") for m in msgs(r, "FAIL")), r.issues)

    def test_retry_recovers_from_dropped_connections(self):
        Handler.drops = 2
        r = self.check("/drop", retries=2)
        self.assertEqual(r.status, 200, r.issues)

    def test_retry_gives_up(self):
        Handler.drops = 5
        r = self.check("/drop", retries=1)
        Handler.drops = 0
        self.assertEqual(r.verdict, "FAIL")

    def test_slow_page_is_warn(self):
        r = self.check("/slow", slow_ms=100)
        self.assertTrue(any(m.startswith("lento") for m in msgs(r, "WARN")), r.issues)

    def test_expect_and_forbid_text(self):
        r = self.check("/ok", site={"expect_text": ["assente xyz"], "forbid_text": ["CONTENUTO di prova"]})
        fails = msgs(r, "FAIL")
        self.assertIn("testo atteso assente: 'assente xyz'", fails)
        self.assertIn("testo vietato presente: 'CONTENUTO di prova'", fails)

    def test_expect_host(self):
        r = self.check("/ok", site={"expect_host": "example.org"})
        self.assertTrue(any("atteso example.org" in m for m in msgs(r, "FAIL")), r.issues)


class TlsDiagnosis(unittest.TestCase):
    def wrap(self, verify_message):
        inner = ssl.SSLCertVerificationError(1, "x")
        inner.verify_message = verify_message
        outer = httpx.ConnectError("")
        outer.__cause__ = inner
        return outer

    def test_messages(self):
        cases = {
            "certificate has expired": "scaduto",
            "Hostname mismatch, certificate is not valid for 'x'": "non corrisponde",
            "self-signed certificate": "self-signed",
            "self-signed certificate in certificate chain": "radice",
            "unable to get local issuer certificate": "catena",
        }
        for vm, word in cases.items():
            self.assertIn(word, sc.tls_error(self.wrap(vm)), vm)

    def test_not_tls(self):
        self.assertIsNone(sc.tls_error(httpx.ConnectError("refused")))


METRICS = dict(title="Benvenuti da Acme", text="x" * 500, textLen=500, words=120, sheets=2, declared=2, inline=0, h1Count=1,
               h1Text="Benvenuto", linkRatio=0.2, password=False, viewportMeta=True, lowContrast=[], overlay=0.0,
               offender=None, overflow=0, brokenImgs=[], links=[])


def review(vp="desktop", sub=False, **over):
    return sc.review_metrics({**METRICS, **over}, vp, sub, 40)


class ReviewMetrics(unittest.TestCase):
    def test_healthy(self):
        self.assertEqual(review(), [])

    def test_css_declared_but_not_loaded(self):
        self.assertEqual(review(sheets=0)[0][0], "FAIL")

    def test_empty_page(self):
        self.assertIn(("FAIL", "pagina vuota nel browser"), review(textLen=10))

    def test_error_title(self):
        self.assertTrue(any(s == "FAIL" and "errore" in m for s, m in review(title="404 Not Found")))

    def test_overlay_and_overflow(self):
        self.assertTrue(any("copre" in m for _, m in review(overlay=0.9)))
        self.assertTrue(any("overflow" in m for _, m in review("mobile", overflow=50)))
        self.assertFalse(any("overflow" in m for _, m in review("desktop", overflow=50)))

    def test_thin_content_and_login(self):
        self.assertTrue(any("poco contenuto" in m for _, m in review(words=10)))
        self.assertTrue(any("login" in m for _, m in review(password=True, words=30)))


class PickLinks(unittest.TestCase):
    def cands(self, *hrefs):
        return [{"href": h, "nav": False} for h in hrefs]

    def test_diversifies_sections_and_skips_language_prefix(self):
        out = sc.pick_links(self.cands("/en/a/1", "/en/a/2", "/en/b/1", "/en/c/1"), "https://x.it/", 3, [])
        self.assertEqual(out, ["https://x.it/en/a/1", "https://x.it/en/b/1", "https://x.it/en/c/1"])

    def test_excludes_external_files_and_base(self):
        out = sc.pick_links(self.cands("https://altro.it/", "/doc.pdf", "/logout", "/", "/ok"), "https://x.it/", 5,
                            [r"\.pdf", "logout"])
        self.assertEqual(out, ["https://x.it/ok"])


class ConfigAndHistory(unittest.TestCase):
    def mk(self, url, issues, ms=100):
        r = sc.Result(url=url, ms=ms)
        r.issues = issues
        return r

    def run_of(self, *rs):
        return {"sites": sc.snapshot(rs)}

    def test_parse_site(self):
        s = sc.parse_site({"url": "a.it", "expect_text": "ciao"}, "t")
        self.assertEqual((s["url"], s["expect_text"], s["forbid_text"]), ("https://a.it", ["ciao"], []))

    def test_parse_site_unknown_key(self):
        with self.assertRaises(SystemExit):
            sc.parse_site({"url": "a.it", "expct_text": "x"}, "t")

    def test_diff_transitions(self):
        prev = [self.run_of(self.mk("u", []))]
        ch = sc.diff_runs([self.mk("u", [("FAIL", "HTTP 500")])], prev)
        self.assertEqual((ch[0]["kind"], ch[0]["fail"]), ("peggiora", True))
        self.assertIn("HTTP 500", ch[0]["msg"])
        prev = [self.run_of(self.mk("u", [("FAIL", "x")]))]
        self.assertEqual(sc.diff_runs([self.mk("u", [])], prev)[0]["kind"], "migliora")
        self.assertEqual(sc.diff_runs([self.mk("u", [])], [self.run_of(self.mk("u", []))]), [])

    def test_diff_slowdown_needs_history(self):
        runs = [self.run_of(self.mk("u", [], 200)) for _ in range(3)]
        self.assertEqual(sc.diff_runs([self.mk("u", [], 2000)], runs)[0]["kind"], "lento")
        self.assertEqual(sc.diff_runs([self.mk("u", [], 2000)], runs[:2]), [])

    def test_alert_modes(self):
        bad, ok = self.mk("u", [("FAIL", "HTTP 500")]), self.mk("v", [])
        self.assertIn("HTTP 500", sc.build_alert([bad, ok], [], "fail"))
        self.assertIsNone(sc.build_alert([ok], [], "fail"))
        self.assertIsNone(sc.build_alert([bad], [], "change"))  # senza cambi di stato non avvisa
        ch = [{"url": "u", "kind": "peggiora", "fail": True, "msg": "u: OK -> FAIL"}]
        self.assertIn("u: OK -> FAIL", sc.build_alert([bad], ch, "change"))

    def test_history_trim_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "h.jsonl"
            for _ in range(5):
                sc.append_history(f, [self.mk("u", [])], keep=3)
            self.assertEqual(len(sc.read_history(f, 10)), 3)


if __name__ == "__main__":
    unittest.main()
