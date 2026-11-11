#!/usr/bin/env python3
"""Render publication previews with synthetic data, never a network quote service.

Prefer the app's loopback hook. If listeners are sandboxed, replace only its HTTP
module in an isolated app copy with a deterministic local Starlark double. The
published source is never rewritten. Requires an explicit non-live PIXLET path.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
PUBLISH = ROOT / "publish/tronbyt/apps/stockwatchlist"
TICKERS = ("SPY", "QQQ", "AAPL", "MSFT", "NVDA", "GLD", "DIA", "TLT")
INDEX_DATA = {
    "DJI": {"price": 42000, "change": 123.4, "change_percentage": 0.3},
    "SPX": {"price": 5700, "change": -12.3, "change_percentage": -0.2},
    "NDX": {"price": 20000, "change": 0, "change_percentage": 0},
    "RUT": {"price": 2200, "change": 18.6, "change_percentage": 0.9},
    "VIX": {"price": 18.4, "change": 0.4, "change_percentage": 2.2},
}
QUOTE_DATA = {symbol: {
    "price": 100 + i * 17, "change_percentage": (1 if i % 2 == 0 else -1) * (0.3 + i / 10),
    "previous_close": 99 + i * 17, "market_status": "open", "quote_status": "delayed",
    "session_date": "2026-11-02", "as_of": "2026-11-02T10:00:00-05:00",
} for i, symbol in enumerate(TICKERS)}
RESPONSE_HEADERS = {"X-Ratelimit-Requests-Limit": "100000",
                    "X-Ratelimit-Requests-Remaining": "90000"}


class PreviewHTTP(BaseHTTPRequestHandler):
    def do_GET(self):
        endpoint = urlsplit(self.path).path
        if endpoint not in ("/mock/v1/quotes", "/mock/v1/indices"):
            self.send_error(404)
            return
        data = INDEX_DATA if endpoint.endswith("indices") else QUOTE_DATA
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        for name, value in RESPONSE_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(json.dumps({"data": data}).encode())

    def log_message(self, *args):
        pass


def local_http_module():
    # All payloads and the key below are visibly synthetic. No http.star import.
    return '''load("encoding/json.star", "json")

QUOTES = json.decode(%s)
INDICES = json.decode(%s)

def get(url, params, headers, ttl_seconds):
    if url not in ("http://127.0.0.1:12345/mock/v1/quotes", "http://127.0.0.1:12345/mock/v1/indices"):
        fail("preview must use the synthetic loopback endpoint")
    if headers.get("X-RapidAPI-Key") != "synthetic-preview" or headers.get("X-RapidAPI-Host") != "us-stock-etf-prices.p.rapidapi.com":
        fail("unexpected preview headers")
    data = INDICES if url.endswith("indices") else QUOTES
    payload = json.encode({"data": {s: data[s] for s in params["symbols"].split(",")}})
    def body():
        return payload
    print("PREVIEW_MOCK " + ("indices" if url.endswith("indices") else "quotes"))
    return struct(status_code = 200, headers = %s, body = body)

http = struct(get = get)
''' % (json.dumps(json.dumps(QUOTE_DATA)), json.dumps(json.dumps(INDEX_DATA)),
       json.dumps(RESPONSE_HEADERS))


def main():
    pixlet = os.environ.get("PIXLET", "")
    found = shutil.which(pixlet) if pixlet else None
    resolved = Path(found).resolve() if found else None
    if resolved is None or ".stockwatchlist" in Path(pixlet).parts or ".stockwatchlist" in resolved.parts:
        raise SystemExit("Set PIXLET to an executable outside the live installation")
    pixlet = str(resolved)
    server, thread = None, None
    try:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), PreviewHTTP)
        except PermissionError:
            mode = "local Starlark HTTP double (sandbox blocked loopback)"
        else:
            mode = "loopback HTTP server"
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
        with tempfile.TemporaryDirectory(prefix=".run-preview-", dir=ROOT / "tests") as scratch:
            work = Path(scratch)
            app = work / "stockwatchlist"
            app.mkdir()
            source = (ROOT / "stock_watchlist.star").read_text()
            if server is None:
                source = source.replace('load("http.star", "http")', 'load("preview_http.star", "http")', 1)
                (app / "preview_http.star").write_text(local_http_module())
            (app / "stock_watchlist.star").write_text(source)
            shutil.copyfile(PUBLISH / "manifest.yaml", app / "manifest.yaml")
            config = {
                "rapidapi_key": "synthetic-preview", "symbols": ",".join(TICKERS),
                "show_indices": "true", "fake_now_et": "2026-11-02T10:15",
                "api_base": "http://127.0.0.1:%d/mock" % (server.server_port if server else 12345),
            }
            config_path = work / "preview.json"
            config_path.write_text(json.dumps(config))
            env = dict(os.environ, HOME=str(work), TMPDIR=str(work), XDG_CACHE_HOME=str(work),
                       XDG_CONFIG_HOME=str(work))
            env.pop("PIXLET_REDIS_URL", None)
            for fmt in ("webp", "gif"):
                output = app / ("stockwatchlist." + fmt)
                args = [pixlet, "render", str(app), "-c", str(config_path), "--output", str(output)]
                args += ["-z", "9"] if fmt == "webp" else ["--format", "gif", "--magnify", "10"]
                result = subprocess.run(args, cwd=work, env=env, text=True, capture_output=True, timeout=30)
                if result.returncode:
                    raise RuntimeError(result.stdout + result.stderr)
                if not output.is_file():
                    raise RuntimeError("preview render produced no image")
                shutil.copyfile(output, PUBLISH / output.name)
                print(result.stdout.strip())
                print("Rendered %s (%d bytes)" % (output.name, output.stat().st_size))
            print("Preview source: " + mode)
    finally:
        if server:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    main()
