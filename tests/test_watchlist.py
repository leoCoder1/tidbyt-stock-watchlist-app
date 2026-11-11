#!/usr/bin/env python3
"""Integration tests: real Pixlet, synthetic quotes, loopback HTTP, no credentials."""
import collections
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
INDEX_SYMBOLS = ["DJI", "SPX", "NDX", "RUT", "VIX"]
PIXLET = os.environ.get("PIXLET", "")
SYMBOLS = ["T" + chr(65 + i // 26) + chr(65 + i % 26) for i in range(105)]
NOW = "2026-11-02T10:15"
APP_SOURCE = (ROOT / "stock_watchlist.star").read_text()
UPGRADE_URL = re.search(r'^UPGRADE_URL = "([^"]+)"', APP_SOURCE, re.M)[1]
UPGRADE_SCROLL_MS = int(re.search(r'^UPGRADE_SCROLL_MS = (\d+)', APP_SOURCE, re.M)[1])
UPGRADE_FRAME_MS = int(re.search(r'^UPGRADE_FRAME_MS = (\d+)', APP_SOURCE, re.M)[1])


class MockQuotes(BaseHTTPRequestHandler):
    calls = collections.defaultdict(list)
    requests = collections.defaultdict(list)
    headers_seen = collections.defaultdict(list)
    request_times = collections.defaultdict(list)
    rate_limited = collections.Counter()

    def do_GET(self):
        request = urlsplit(self.path)
        scenario = request.path.split("/")[1]
        endpoint = request.path.rsplit("/", 1)[-1]
        if endpoint not in ("quotes", "indices"):
            self.send_error(404)
            return
        symbols = parse_qs(request.query).get("symbols", [""])[0].split(",")
        self.requests[scenario].append((endpoint, symbols))
        self.headers_seen[scenario].append({k: self.headers.get(k) for k in ("X-RapidAPI-Key", "X-RapidAPI-Host")})
        if endpoint == "quotes":
            self.calls[scenario].append(symbols)
        now = time.monotonic()
        recent = [t for t in self.request_times[scenario] if now - t < 1]
        self.request_times[scenario] = recent + [now]
        per_second = scenario in ("unknownpersecond", "paidpersecond")
        zero_remaining = scenario.endswith("zero")
        limited = (scenario.startswith("rate") and len(recent) >= 5) or scenario == "unknownburst" or per_second
        self.rate_limited[scenario] += limited
        if scenario.startswith("slow"):
            time.sleep(1.6)
        quota = scenario.startswith(("quota", "freequota")) or (scenario == "unknownquota" and len(self.calls[scenario]) == 6)
        quota = quota or (scenario.startswith(("paidmarquee", "unknownmarquee")) and len(self.calls[scenario]) >= 2)
        origin_quota = scenario == "originquota"
        free = scenario.startswith("free") or (scenario == "unknownbasic" and len(self.calls[scenario]) > 5)
        error = endpoint == "indices" and scenario == "indiceserror"
        auth = re.fullmatch(r"auth-(quotes|indices)-(bare|gateway)(-later|-empty)?-(401|403)", scenario)
        auth_error = auth and endpoint == auth[1] and (not auth[3] or len(self.requests[scenario]) > 1)
        self.send_response(int(auth[4]) if auth_error else 502 if error else 429 if quota or limited else 200)
        self.send_header("Content-Type", "application/json")
        if not origin_quota and (not scenario.startswith("unknown") or free):
            self.send_header("X-Ratelimit-Requests-Limit", "5" if free else "100000")
        if not origin_quota and (not scenario.startswith("unknown") or free or quota or per_second or zero_remaining):
            self.send_header("X-Ratelimit-Requests-Remaining", "0" if quota or zero_remaining else "4" if free else "90000")
            self.send_header("X-Ratelimit-Requests-Reset", "3600")
        if per_second:
            self.send_header("X-RateLimit-Remaining", "0")
            self.send_header("X-RateLimit-Reset", "1")
        self.end_headers()
        if auth_error:
            body = (json.dumps({"message": "Synthetic rejected key"}).encode() if auth[2] == "gateway"
                    else b"" if auth[4] == "401" else b"<html>Forbidden</html>")
            self.wfile.write(body)
            return
        if origin_quota:
            body = {"data": {}, "error": "daily quota exceeded"}
        elif error:
            body = {"error": "Synthetic unavailable indices"}
        elif per_second:
            body = {"message": "Per-second quota exceeded; retry in one second"}
        elif quota or limited:
            body = {"message": "Synthetic quota exhausted" if quota else "Synthetic rate limit: 5 requests/second"}
        elif auth and auth[3] == "-empty" and endpoint == "quotes":
            body = {"data": {}}
        elif endpoint == "indices":
            body = {"data": {s: {
                "price": {"DJI": 42000, "SPX": 5700, "NDX": 20000, "RUT": 2200, "VIX": 18.4}[s],
                "change_percentage": -0.5, "change": -10, "previous_close": 20010,
                "name": s, "data_updated": True, "is_stale": scenario == "indicesstale",
                "as_of": "2026-11-02T10:00:00-05:00",
            } for s in symbols}}
            if scenario == "indicespoints":
                for s, change, pct in (("DJI", 245.2, 0.5), ("SPX", -38.4, -0.7), ("NDX", 0, 0),
                                       ("RUT", 18.6, 0.9), ("VIX", 0.4, 2.2)):
                    body["data"][s].update(price=47821, change=change, change_percentage=pct)
            elif scenario == "indicesfallback":
                for s, previous in (("DJI", 41990), ("SPX", 5703.4), ("NDX", 20000),
                                    ("RUT", 2195.6), ("VIX", 18.0)):
                    body["data"][s].pop("change")
                    body["data"][s]["previous_close"] = previous
            elif scenario == "indicesmissing":
                for s in symbols:
                    body["data"][s].pop("change")
                    body["data"][s].pop("previous_close")
                body["data"]["SPX"].update(change="bad", previous_close="bad")
                body["data"]["NDX"].update(change=None, previous_close=None)
            elif scenario == "indicesbadprice":
                for s in symbols:
                    body["data"][s].update(price="bad", change=None)
            elif scenario == "indicestiny":
                for s, change in (("DJI", 9.2), ("SPX", 0.001), ("NDX", -0.001), ("RUT", 18.6), ("VIX", 0.4)):
                    body["data"][s]["change"] = change
            elif scenario.startswith(("indicesomit", "indicesinvalid")):
                missing = ("RUT", "VIX") if scenario.endswith("both") else ("RUT",) if scenario.endswith("rut") else ("VIX",)
                for symbol in missing:
                    if scenario.startswith("indicesomit"):
                        del body["data"][symbol]
                    else:
                        body["data"][symbol] = {"error": "Synthetic unavailable index", "price": None,
                                                "change": None, "change_percentage": None}
        else:
            body = {"data": {s: {
                "price": 100 + SYMBOLS.index(s),
                "change_percentage": 1.0,
                "change": -99,
                "previous_close": 99,
                "market_status": "closed" if scenario.startswith("closed") else "open",
                "quote_status": "closed" if scenario.startswith("closed") else "delayed",
                "session_date": "2026-11-02",
                "as_of": "2026-11-02T10:00:00-05:00",
            } for s in symbols}}
        if zero_remaining:
            body["message"] = "daily quota exhausted"
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *args):
        pass


def walk(node):
    yield node
    for child in node["children"]:
        yield from walk(child)


def texts(node):
    return [n["props"]["content"] for n in walk(node) if n["kind"] == "Text"]


def displayed(tree):
    return [t for t in texts(tree) if t in SYMBOLS]


def pages(tree):
    seq = next((n for n in walk(tree) if n["kind"] == "Sequence"), None)
    if seq:
        out = []
        for child in seq["children"]:
            if child["kind"] == "Animation" and child["children"]:
                out.append(child["children"][0])
            else:
                out.append(child)
        return out
    return next((n["children"] for n in walk(tree) if n["kind"] == "Animation"), tree["children"])


def frame_durations(path):
    """Read WebP animation timing with the standard library (no image dependencies)."""
    raw = path.read_bytes()
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WEBP", "Pixlet did not produce a WebP"
    durations = []
    pos = 12
    while pos + 8 <= len(raw):
        kind, size = struct.unpack_from("<4sI", raw, pos)
        if kind == b"ANMF":
            durations.append(int.from_bytes(raw[pos + 20:pos + 23], "little"))
        pos += 8 + size + (size & 1)
    return durations


class WatchlistTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not PIXLET:
            raise unittest.SkipTest("PIXLET unset; real Pixlet/HTTP/encoder tests were NOT RUN")
        # The user's launchd executable may live here; this task must never touch it.
        if ".stockwatchlist" in Path(PIXLET).parts:
            raise unittest.SkipTest("Configured PIXLET is under protected ~/.stockwatchlist; Pixlet/HTTP/encoder tests were NOT RUN")
        if not shutil.which(PIXLET):
            raise unittest.SkipTest("Pixlet not found; real Pixlet/HTTP/encoder tests were NOT RUN")
        help_result = subprocess.run([PIXLET, "render", "--help"], text=True,
                                     capture_output=True, check=True)
        cls.help_text = help_result.stdout + help_result.stderr
        duration_flag = re.search(r"(--[a-z-]*duration[a-z-]*)\s+(\w+)", cls.help_text)
        if not duration_flag:
            raise RuntimeError("Pixlet must support a maximum animation duration flag")
        cls.duration_flag, cls.duration_type = duration_flag.groups()
        try:
            cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockQuotes)
        except PermissionError:
            raise unittest.SkipTest("Sandbox disallows loopback listeners; real Pixlet/HTTP/encoder tests were NOT RUN")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.runtime = tempfile.TemporaryDirectory(prefix=".run-", dir=ROOT / "tests")
        cls.work = Path(cls.runtime.name)
        source = (ROOT / "stock_watchlist.star").read_text()
        source = source.replace("def main(config):", "def app_main(config, test_symbols = None):", 1)
        source = source.replace('parse_symbols(config.str("symbols", "") or DEFAULT_SYMBOLS)',
                                'parse_symbols(test_symbols if test_symbols != None else (config.str("symbols", "") or DEFAULT_SYMBOLS))', 1)
        for widget in ("Root", "Row", "Box", "Text", "Column", "Padding", "Animation", "Marquee", "Sequence"):
            source = source.replace("render." + widget + "(", "test_" + widget.lower() + "(")
        source = source.replace("cache.set(", "test_cache_set(")
        cls.app = cls.work / "watchlist.star"
        cls.app.write_text(source + "\n" + (ROOT / "tests/trace.star").read_text())

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.runtime.cleanup()

    def render(self, scenario, count, renders=1, dwell=15000, raw=False, symbols=None, symbol_lists=None, state=None,
               show_indices=False, key=None, omit_key=False):
        output = self.work / (scenario + ("-raw" if raw else "") + ".webp")
        app = ROOT / "stock_watchlist.star" if raw else self.app
        run_dir = Path(tempfile.mkdtemp(prefix="render-", dir=self.work))
        duration = str(dwell) if self.duration_type in ("int", "uint", "uint32", "uint64") else str(dwell) + "ms"
        args = [PIXLET, "render", str(app), "--output", str(output),
                self.duration_flag, duration,
                "symbols=" + (symbols or ",".join(SYMBOLS[:count])),
                "fake_now_et=" + NOW,
                "api_base=http://127.0.0.1:%d/%s" % (self.server.server_port, scenario),
                "test_renders=" + str(renders),
                "test_lists=" + json.dumps(symbol_lists or [])]
        if not omit_key:
            args.append("rapidapi_key=" + ("test-" + scenario if key is None else key))
        if show_indices is not None:
            args.append("show_indices=" + str(show_indices).lower())
        args += [k + "=" + str(v) for k, v in (state or {}).items()]
        if re.search(r"--cache(?:\s|$)", self.help_text):
            args.append("--cache")
        env = dict(os.environ, HOME=str(run_dir), TMPDIR=str(run_dir), XDG_CACHE_HOME=str(run_dir), XDG_CONFIG_HOME=str(run_dir))
        env.pop("PIXLET_REDIS_URL", None)  # never connect to a user's persistent cache
        started = time.monotonic()
        result = subprocess.run(args, cwd=run_dir, env=env, text=True, capture_output=True, timeout=30)
        self.last_elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = (result.stdout + result.stderr).splitlines()
        matches = [re.search(r"STATE page_offset=(\d+) plan_limit=(\d+)$", line) for line in lines]
        self.last_states = [dict(zip(("page_offset", "plan_limit"), m.groups())) for m in matches if m]
        self.assertEqual(len(self.last_states), renders, result.stdout + result.stderr)
        self.last_saves = [json.loads(line.split("TEST_CACHE:", 1)[1]) for line in lines if "TEST_CACHE:" in line]
        self.last_cache_writes = [line.split("TEST_WRITE:", 1)[1] for line in lines if "TEST_WRITE:" in line]
        trees = [json.loads(line.split("TEST_TREE:", 1)[1])
                 for line in lines if "TEST_TREE:" in line]
        if not raw:
            self.assertEqual(len(trees), renders, result.stdout + result.stderr)
            for tree in trees:
                count_pages = len(pages(tree))
                delay = tree["props"].get("delay", dwell)
                if count_pages > 1:
                    scrolling_pages = sum(any(n["kind"] == "Marquee" for n in walk(page)) for page in pages(tree))
                    minimum = min(1500, dwell // 2) if show_indices is not False else 1500
                    if scrolling_pages:
                        self.assertEqual(delay, UPGRADE_FRAME_MS)
                        self.assertLessEqual((count_pages - scrolling_pages) * minimum + scrolling_pages * UPGRADE_SCROLL_MS, dwell)
                    else:
                        self.assertGreaterEqual(delay, minimum)
                        self.assertLessEqual(count_pages * delay, dwell)
        durations = frame_durations(output)
        self.last_durations = durations
        if durations:
            minimum = min(1500, dwell // 2) if show_indices is not False else 1500
            has_marquee = not raw and any(n["kind"] == "Marquee" for n in walk(trees[-1]))
            if has_marquee:
                # Scroll frames have their own short delay. Every static ticker/indices
                # page must still survive encoding with a readable hold time.
                static_pages = sum(not any(n["kind"] == "Marquee" for n in walk(page)) for page in pages(trees[-1]))
                self.assertGreaterEqual(sum(d >= minimum for d in durations), static_pages)
            else:
                self.assertGreaterEqual(min(durations), minimum)
            self.assertLessEqual(sum(durations), dwell)
        return trees

    def assert_requests(self, scenario, expected):
        calls = MockQuotes.calls[scenario]
        self.assertEqual(len(calls), (len(expected) + 4) // 5, calls)
        self.assertTrue(all(1 <= len(c) <= 5 for c in calls))
        self.assertEqual([s for c in calls for s in c], expected)

    def test_sizes_fetch_every_chunk_and_eventually_display_every_ticker(self):
        for count in (5, 25, 26, 50, 100):
            with self.subTest(tickers=count):
                scenario = "paid%d" % count
                trees = self.render(scenario, count, renders=5)
                self.assert_requests(scenario, SYMBOLS[:count])
                seen = set()
                for tree in trees:
                    shown = displayed(tree)
                    self.assertTrue(shown)
                    self.assertNotIn("no data", texts(tree))
                    seen.update(shown)
                    self.assertNotIn("more", texts(tree))
                    self.assertFalse(any(re.fullmatch(r"\d+-\d+/\d+", t) for t in texts(tree)))
                self.assertEqual(seen, set(SYMBOLS[:count]))

    def test_paging_continues_on_next_render_and_wraps(self):
        trees = self.render("continuity", 100, renders=6)
        self.assert_requests("continuity", SYMBOLS[:100])
        first, second = map(displayed, trees[:2])
        self.assertLess(len(first), 100)
        self.assertEqual(first + second, SYMBOLS[:len(first) + len(second)])
        stream = [s for tree in trees for s in displayed(tree)]
        self.assertEqual(stream, (SYMBOLS[:100] * 3)[:len(stream)])

    def test_closed_market_fills_every_window_with_persistent_cache(self):
        scenario = "closedpersistent"
        trees = self.render(scenario, 100, renders=8)
        self.assert_requests(scenario, SYMBOLS[:100])
        for batch, tree in enumerate(trees):
            expected = SYMBOLS[(batch % 4) * 25:(batch % 4 + 1) * 25]
            self.assertEqual(displayed(tree), expected)
            self.assertNotIn("no data", texts(tree))
            for symbol in expected:
                self.assertIn(str(100 + SYMBOLS.index(symbol)), texts(tree))
        self.assertEqual([s for tree in trees for s in displayed(tree)], SYMBOLS[:100] * 2)

    def test_new_status_at_minimum_dwell_never_refetches_hidden_rows(self):
        scenario = "closedshort"
        trees = self.render(scenario, 100, renders=3, dwell=1500, state={"plan_limit": "100000"})
        for batch, tree in enumerate(trees):
            self.assertEqual(displayed(tree), SYMBOLS[batch * 4:(batch + 1) * 4])
            self.assertNotIn("no data", texts(tree))
            self.assertFalse(any(t.startswith("CLOSED") for t in texts(tree)))
            self.assertFalse(any(re.fullmatch(r"\d+-\d+/\d+", t) for t in texts(tree)))
        fetched = [s for c in MockQuotes.calls[scenario] for s in c]
        self.assertEqual(fetched, [s for tree in trees for s in displayed(tree)])
        self.assertEqual(fetched, SYMBOLS[:12])
        self.assertEqual([s["page_offset"] for s in self.last_states], ["4", "8", "12"])

    def test_successful_basic_fetches_only_first_chunk_without_upgrade_notice(self):
        trees = self.render("free", 100, renders=2)
        self.assert_requests("free", SYMBOLS[:5])
        for tree in trees:
            self.assertEqual(displayed(tree), SYMBOLS[:5])
            self.assertFalse(any("PRO" in t or "NEED" in t for t in texts(tree)))
            self.assertNotIn(UPGRADE_URL, texts(tree))

    def test_quota_429_pauses_following_render(self):
        trees = self.render("quota", 100, renders=2)
        self.assertEqual(MockQuotes.calls["quota"], [SYMBOLS[:5]])
        for tree in trees:
            self.assertIn("QUOTA USED UP", texts(tree))
            self.assertIn(UPGRADE_URL, texts(tree))
            self.assertNotIn("more", texts(tree))

    def test_notices_fit_with_short_dwell(self):
        for dwell in (4500, 3000, 1500):
            with self.subTest(dwell=dwell):
                scenario = "freequota%d" % dwell
                trees = self.render(scenario, 100, renders=2, dwell=dwell, state={"plan_limit": "5"})
                self.assert_requests(scenario, SYMBOLS[:5])
                for tree in trees:
                    self.assertIn("FREE: 5/DAY USED", texts(tree))
                    self.assertNotIn("+95 TICKERS NEED", texts(tree))
                    self.assertTrue(displayed(tree))
                    self.assertNotIn(UPGRADE_URL, texts(tree))
                    self.assertFalse(any(n["kind"] == "Marquee" for n in walk(tree)))
                self.assertTrue(all(s["page_offset"] == "0" for s in self.last_states))

    def test_success_with_zero_remaining_has_no_quota_or_upgrade_notices(self):
        for indices in (False, True):
            for plan, expected_limit in (("unknown", "0"), ("free", "5"), ("paid", "100000")):
                with self.subTest(indices=indices, plan=plan):
                    scenario = plan + ("indices" if indices else "") + "zero"
                    trees = self.render(scenario, 100, renders=2, show_indices=indices)
                    self.assertTrue(all(s["plan_limit"] == expected_limit for s in self.last_states))
                    count = 5 if plan == "free" else 20 if indices else 25
                    self.assertEqual(displayed(trees[0]), SYMBOLS[:count])
                    # Two paid/unknown renders issue 5 requests each, except a cached
                    # indices URL may be served as a Pixlet HTTP HIT on the second render.
                    expected = 1 if plan == "free" else 9 if indices else 10
                    self.assertEqual(len(MockQuotes.requests[scenario]), expected)
                    for tree in trees:
                        self.assertFalse(any("PRO" in t or "QUOTA" in t or "USED" in t or "RATE LIMITED" in t
                                             for t in texts(tree)))
                        self.assertNotIn(UPGRADE_URL, texts(tree))
                        if indices and plan != "free":
                            self.assertNotIn("no data", texts(pages(tree)[0]))

    def test_free_plan_with_stale_offset_always_fetches_first_chunk(self):
        scenario = "freeoffset"
        tree, = self.render(scenario, 100, state={"page_offset": "60", "fetch_cursor": "12", "plan_limit": "5"})
        self.assert_requests(scenario, SYMBOLS[:5])
        self.assertEqual(displayed(tree), SYMBOLS[:5])
        self.assertNotIn("no data", texts(tree))
        self.assertEqual(self.last_states[-1]["page_offset"], "0")

    def test_rate_limited_mock_never_receives_a_sixth_request(self):
        self.render("ratelimit", 100)
        self.assert_requests("ratelimit", SYMBOLS[:25])
        self.assertEqual(MockQuotes.rate_limited["ratelimit"], 0)

    def test_slow_requests_save_each_partial_chunk_within_budget(self):
        self.render("slow", 100)
        self.assertLess(self.last_elapsed, 15)
        self.assert_requests("slow", SYMBOLS[:25])
        self.assertEqual(len(self.last_saves), 5)
        for i, saved in enumerate(self.last_saves, 1):
            self.assertEqual(set(saved) - {"_old"}, set(SYMBOLS[:i * 5]))

    def test_state_carries_paging_and_fetches_across_separate_processes(self):
        for count in (50, 100):
            with self.subTest(tickers=count):
                scenario = "processes%d" % count
                state = {"page_offset": "0", "plan_limit": "0"}
                seen = []
                for batch in range(count // 25):
                    before = len(MockQuotes.calls[scenario])
                    tree, = self.render(scenario, count, state=state)
                    calls = MockQuotes.calls[scenario][before:]
                    self.assertEqual(len(calls), 5)
                    self.assertEqual(displayed(tree), [s for c in calls for s in c])
                    self.assertNotIn("no data", texts(tree))
                    self.assertEqual(len(displayed(tree)), 25)
                    state = self.last_states[-1]
                    self.assertEqual(state["page_offset"], str(((batch + 1) * 25) % count))
                    seen += displayed(tree)
                self.assertEqual(seen, SYMBOLS[:count])
                self.assert_requests(scenario, SYMBOLS[:count])

    def test_small_dwell_fetches_only_rows_shown_across_fresh_processes(self):
        for dwell, capacity in ((1500, 4), (3000, 8), (4500, 12)):
            with self.subTest(dwell=dwell):
                scenario = "short%d" % dwell
                state = {"page_offset": "17", "plan_limit": "100000"}
                for batch in range(2):
                    before = len(MockQuotes.calls[scenario])
                    tree, = self.render(scenario, 100, dwell=dwell, state=state)
                    calls = MockQuotes.calls[scenario][before:]
                    self.assertEqual(len(calls), (capacity + 4) // 5)
                    self.assertEqual(displayed(tree), SYMBOLS[17 + batch * capacity:17 + (batch + 1) * capacity])
                    self.assertEqual(displayed(tree), [s for c in calls for s in c])
                    self.assertNotIn("no data", texts(tree))
                    state = self.last_states[-1]

    def test_paid_pause_notices_keep_paging_at_minimum_dwells(self):
        for dwell in (1500, 3000):
            with self.subTest(dwell=dwell):
                trees = self.render("quota%d" % dwell, 100, renders=2, dwell=dwell)
                self.assertTrue(all(displayed(t) for t in trees))
                self.assertNotEqual(displayed(trees[0])[0], displayed(trees[1])[0])
                self.assertNotEqual(self.last_states[0]["page_offset"], self.last_states[1]["page_offset"])

    def test_only_symbols_over_100_are_truncated(self):
        trees = self.render("overflow", 105, renders=5)
        self.assert_requests("overflow", SYMBOLS[:100])
        shown_text = [t for tree in trees for t in texts(tree)]
        self.assertIn("+5", shown_text)
        self.assertIn("more", shown_text)
        self.assertFalse(set(SYMBOLS[100:]) & set(shown_text))

    def test_symbol_lists_have_independent_page_positions(self):
        forward = ",".join(SYMBOLS[:100])
        reverse = ",".join(reversed(SYMBOLS[:100]))
        first, other, continued = self.render("listpositions", 100, renders=3,
                                              symbol_lists=[forward, reverse, forward])
        self.assertEqual(displayed(first)[0], SYMBOLS[0])
        self.assertEqual(displayed(other)[0], SYMBOLS[99])
        self.assertEqual(displayed(continued)[0], SYMBOLS[len(displayed(first))])
        self.assertEqual([s for c in MockQuotes.calls["listpositions"] for s in c],
                         SYMBOLS[:25] + list(reversed(SYMBOLS[:100]))[:25] + SYMBOLS[25:50])

    def test_uninstrumented_app_renders(self):
        self.render("smoke", 100, raw=True)
        self.assert_requests("smoke", SYMBOLS[:25])

    def test_indices_default_on_and_paid_request_cap(self):
        for toggle, scenario in ((None, "indicesdefault"), (True, "rateindices")):
            with self.subTest(toggle=toggle):
                tree, = self.render(scenario, 100, show_indices=toggle)
                page = pages(tree)[0]
                self.assertEqual(texts(page)[::3], ["DOW", "S&P", "NDX", "RUT", "VIX"])
                self.assertEqual(tuple(page["props"]["pad"]), (0, 1, 0, 0))
                rows = page["children"][0]["children"]
                self.assertEqual(len(rows), 5)
                for row in rows:
                    self.assertEqual([cell["props"]["width"] for cell in row["children"]], [20, 20, 24])
                    self.assertEqual([cell["props"]["height"] for cell in row["children"]], [6, 6, 6])
                self.assertLessEqual(1 + len(rows) * 6, 32)
                self.assertEqual(displayed(tree), SYMBOLS[:20])
                self.assertEqual(self.last_states[-1]["page_offset"], "20")
                self.assertEqual(len(MockQuotes.requests[scenario]), 5)
                self.assertEqual([c for e, c in MockQuotes.requests[scenario] if e == "indices"],
                                 [INDEX_SYMBOLS])
                self.assertEqual(len(MockQuotes.calls[scenario]), 4)
                self.assertEqual(MockQuotes.rate_limited[scenario], 0)
                self.assertFalse(any(t.startswith("CLOSED") for t in texts(tree)))
                self.assertFalse(any(re.fullmatch(r"\d+-\d+/\d+", t) for t in texts(tree)))

    def test_indices_toggle_off_preserves_ticker_offset(self):
        first, = self.render("indicestoggle", 100, show_indices=True)
        state = self.last_states[-1]
        self.assertEqual(displayed(first), SYMBOLS[:20])
        before = len(MockQuotes.requests["indicestoggle"])
        second, = self.render("indicestoggle", 100, state=state, show_indices=False)
        self.assertEqual(displayed(second), SYMBOLS[20:45])
        self.assertEqual(displayed(pages(second)[0]), SYMBOLS[20:25])
        self.assertEqual([e for e, _ in MockQuotes.requests["indicestoggle"][before:]], ["quotes"] * 5)

    def test_basic_indices_page_costs_no_additional_request(self):
        for seed in (False, True):
            with self.subTest(cached=seed):
                scenario = "freeindices" + str(seed)
                state = {"plan_limit": "5"}
                if seed:
                    state["test_indices"] = json.dumps({"DJI": {"price": 42000, "change_percentage": 0.5},
                                                       "SPX": {"price": 5700, "is_stale": True}, "_old": ["DJI"]})
                trees = self.render(scenario, 100, renders=2, state=state, show_indices=True)
                self.assertEqual(MockQuotes.requests[scenario], [("quotes", SYMBOLS[:5])])
                for tree in trees:
                    self.assertEqual(texts(pages(tree)[0])[0], "DOW")
                    self.assertEqual(displayed(tree), SYMBOLS[:5])
                    self.assertFalse(any("PRO" in t or "NEED" in t for t in texts(tree)))
                    self.assertEqual(texts(pages(tree)[0]).count("no data"), 3 if seed else 5)
                    if seed:
                        self.assertEqual(sum(n["props"].get("color") == "#5a5a5a" for n in walk(pages(tree)[0])), 4)

    def test_indices_error_and_stale_payload_leave_quotes_working(self):
        for scenario in ("indiceserror", "indicesstale"):
            with self.subTest(scenario=scenario):
                tree, = self.render(scenario, 100, show_indices=True)
                self.assertEqual(displayed(tree), SYMBOLS[:20])
                self.assertNotIn("no data", [t for page in pages(tree)[1:] for t in texts(page)])
                self.assertEqual(len(self.last_saves), 4)
                if scenario == "indiceserror":
                    self.assertEqual(texts(pages(tree)[0]).count("no data"), 5)
                else:
                    self.assertEqual(sum(n["props"].get("color") == "#5a5a5a" for n in walk(pages(tree)[0])), 10)

    def test_missing_rut_vix_rows_preserve_other_indices_and_quotes(self):
        for suffix, missing in (("rut", ("RUT",)), ("vix", ("VIX",)), ("both", ("RUT", "VIX"))):
            for kind in ("omit", "invalid"):
                scenario = "indices" + kind + suffix
                with self.subTest(scenario=scenario):
                    tree, = self.render(scenario, 100, show_indices=True)
                    rows = pages(tree)[0]["children"][0]["children"]
                    self.assertEqual([texts(row)[0] for row in rows], ["DOW", "S&P", "NDX", "RUT", "VIX"])
                    for symbol, row in zip(INDEX_SYMBOLS, rows):
                        self.assertEqual(texts(row)[1:], ["no data"] if symbol in missing else ["-10", "-0.5%"])
                    self.assertEqual(displayed(tree), SYMBOLS[:20])
                    self.assertEqual(len(MockQuotes.requests[scenario]), 5)
                    self.assertEqual([c for endpoint, c in MockQuotes.requests[scenario] if endpoint == "indices"], [INDEX_SYMBOLS])

    def test_indices_short_dwell_keeps_both_pages_visible(self):
        for dwell in (1500, 3000, 4500):
            tree, = self.render("indicesshort%d" % dwell, 100, dwell=dwell, show_indices=True)
            self.assertEqual(texts(pages(tree)[0])[0], "DOW")
            self.assertTrue(displayed(pages(tree)[1]))

    def test_short_dwell_quota_keeps_indices_and_ticker_pages_without_marquee(self):
        for dwell in (1500, 3000, 4500):
            with self.subTest(dwell=dwell):
                tree, = self.render("quotaindices%d" % dwell, 100, dwell=dwell, show_indices=True)
                self.assertEqual(texts(pages(tree)[0])[0], "DOW")
                self.assertTrue(displayed(pages(tree)[1]))
                self.assertIn("QUOTA USED UP", texts(tree))
                self.assertNotIn(UPGRADE_URL, texts(tree))
                self.assertFalse(any(n["kind"] == "Marquee" for n in walk(tree)))

    def test_missing_headers_fetch_full_window_and_keep_paging(self):
        for indices in (False, True):
            with self.subTest(indices=indices):
                scenario = "unknownindices" if indices else "unknown"
                size = 20 if indices else 25
                first, = self.render(scenario, 100, state={"page_offset": "50"}, show_indices=indices)
                self.assertEqual(displayed(first), SYMBOLS[50:50 + size])
                state = self.last_states[-1]
                self.assertEqual(state["page_offset"], str(50 + size))
                self.assertEqual(state["plan_limit"], "0")
                second, = self.render(scenario, 100, state=state, show_indices=indices)
                self.assertEqual(displayed(second), SYMBOLS[50 + size:50 + 2 * size])
                for tree in (first, second):
                    self.assertFalse(any("PRO" in t or "FREE" in t or "NEED" in t for t in texts(tree)))
                self.assertEqual(len(MockQuotes.requests[scenario]), 10)

    def test_unknown_later_basic_or_quota_applies_first_chunk_policy(self):
        for scenario in ("unknownbasic", "unknownquota"):
            with self.subTest(scenario=scenario):
                first, = self.render(scenario, 100)
                self.assertEqual(displayed(first), SYMBOLS[:25])
                second, = self.render(scenario, 100, state=self.last_states[-1])
                state = self.last_states[-1]
                self.assertEqual(state, {"page_offset": "0", "plan_limit": "5"})
                self.assertEqual(len(MockQuotes.calls[scenario]), 6)
                if scenario == "unknownquota":
                    self.assertTrue(any("USED" in t for t in texts(second)))
                else:
                    self.assertFalse(any("PRO" in t or "NEED" in t for t in texts(second)))
                third, = self.render(scenario, 100, state=state)
                self.assertEqual(MockQuotes.calls[scenario][-1], SYMBOLS[:5])
                self.assertEqual(displayed(third), SYMBOLS[:5])

    def test_burst_429_has_no_quota_copy_or_basic_fallback(self):
        trees = self.render("unknownburst", 100, renders=2)
        self.assertTrue(all(s["plan_limit"] == "0" for s in self.last_states))
        self.assertEqual(MockQuotes.calls["unknownburst"], [SYMBOLS[:5], SYMBOLS[5:10]])
        for tree in trees:
            self.assertIn("RATE LIMITED", texts(tree))
            self.assertFalse(any("PRO" in t or "FREE" in t or "QUOTA" in t or "NEED" in t for t in texts(tree)))
            self.assertNotIn(UPGRADE_URL, texts(tree))

    def test_per_second_quota_429_keeps_unknown_and_paid_plans_without_pause(self):
        for scenario, limit in (("unknownpersecond", "0"), ("paidpersecond", "100000")):
            with self.subTest(plan=limit):
                trees = self.render(scenario, 100, renders=2, state={"plan_limit": limit})
                self.assertTrue(all(s["plan_limit"] == limit for s in self.last_states))
                self.assertEqual(MockQuotes.calls[scenario], [SYMBOLS[:5], SYMBOLS[5:10]])
                for tree in trees:
                    self.assertIn("RATE LIMITED", texts(tree))
                    self.assertFalse(any("PRO" in t or "FREE" in t or "QUOTA" in t for t in texts(tree)))
                    self.assertNotIn(UPGRADE_URL, texts(tree))

    def test_no_key_setup_makes_zero_requests_and_preserves_state(self):
        for i, key in enumerate((None, "", " \n\t")):
            with self.subTest(key=key):
                scenario = "setup%d" % i
                tree, = self.render(scenario, 20, key=key, omit_key=key is None, show_indices=True,
                                    state={"page_offset": "23", "plan_limit": "100000"})
                self.assertEqual(MockQuotes.requests[scenario], [])
                self.assertEqual(self.last_cache_writes, [])
                self.assertEqual(self.last_states, [{"page_offset": "23", "plan_limit": "100000"}])
                self.assertIn("ADD RAPIDAPI KEY", texts(tree))
                self.assertIn("FREE KEY WORKS", texts(tree))
                self.assertIn("FIRST 5 TICKERS", texts(tree))
                marquee, = [n for n in walk(tree) if n["kind"] == "Marquee"]
                self.assertEqual(texts(marquee), [UPGRADE_URL])
                self.assertEqual(marquee["props"]["width"], 64)
                self.assertGreater(len(self.last_durations), 1)
                self.assertGreaterEqual(sum(self.last_durations), 3000)
                self.assertLessEqual(sum(self.last_durations), UPGRADE_SCROLL_MS)

    def test_present_key_keeps_quotes_and_indices_headers(self):
        tree, = self.render("keypresent", 5, key=" synthetic-test \n", show_indices=True)
        self.assertEqual(MockQuotes.requests["keypresent"], [("quotes", SYMBOLS[:5]), ("indices", INDEX_SYMBOLS)])
        self.assertTrue(all(h == {"X-RapidAPI-Key": "synthetic-test", "X-RapidAPI-Host": "us-stock-etf-prices.p.rapidapi.com"}
                            for h in MockQuotes.headers_seen["keypresent"]))
        self.assertEqual(displayed(tree), SYMBOLS[:5])
        self.assertNotIn(UPGRADE_URL, texts(tree))

    def test_bare_auth_errors_show_key_rejected_and_retain_claims(self):
        for endpoint in ("quotes", "indices"):
            for status in (401, 403):
                with self.subTest(endpoint=endpoint, status=status):
                    scenario = "auth-%s-bare-%d" % (endpoint, status)
                    tree, = self.render(scenario, 5, symbols="123,!" if endpoint == "indices" else None,
                                        show_indices=endpoint == "indices", state={"fake_now_et": "2026-11-02T21:00"})
                    self.assertEqual(texts(tree), ["KEY REJECTED", "Wrong key or", "not subscribed", "rapidapi.com"])
                    self.assertEqual(MockQuotes.requests[scenario], [(endpoint, INDEX_SYMBOLS if endpoint == "indices" else SYMBOLS[:5])])
                    self.assertEqual(self.last_cache_writes.count("cold"), 1)  # no release write

    def test_gateway_auth_errors_show_key_rejected_and_release_claims(self):
        for endpoint, plan in (("quotes", "0"), ("quotes", "5"), ("indices", "0")):
            for status in (401, 403):
                with self.subTest(endpoint=endpoint, plan=plan, status=status):
                    scenario = "auth-%s-gateway-%d" % (endpoint, status)
                    trees = self.render(scenario, 5, renders=2, symbols="123,!" if endpoint == "indices" else None,
                                        show_indices=endpoint == "indices",
                                        state={"fake_now_et": "2026-11-02T21:00", "plan_limit": plan})
                    for tree in trees:
                        self.assertEqual(texts(tree), ["KEY REJECTED", "Wrong key or", "not subscribed", "rapidapi.com"])
                    if plan == "5":
                        self.assertEqual(self.last_cache_writes.count("slot"), 4)
                        self.assertEqual(self.last_cache_writes.count("calls"), 4)
                    else:
                        self.assertEqual(self.last_cache_writes.count("cold"), 4)

    def test_auth_errors_after_quotes_keep_data_and_stop_requests(self):
        for endpoint in ("quotes", "indices"):
            for kind in ("bare", "gateway"):
                for status in (401, 403):
                    with self.subTest(endpoint=endpoint, kind=kind, status=status):
                        scenario = "auth-%s-%s-later-%d" % (endpoint, kind, status)
                        tree, = self.render(scenario, 20, show_indices=endpoint == "indices",
                                            state={"fake_now_et": "2026-11-02T21:00"})
                        self.assertEqual(displayed(tree), SYMBOLS[:10 if endpoint == "quotes" else 5])
                        self.assertEqual(set(self.last_saves[-1]) - {"_old"}, set(SYMBOLS[:5]))
                        self.assertNotIn("KEY REJECTED", texts(tree))
                        self.assertEqual(len(MockQuotes.requests[scenario]), 2)
                        self.assertEqual(self.last_cache_writes.count("cold"), 1)

    def test_gateway_auth_error_after_fresh_empty_quotes_retains_claims(self):
        for status in (401, 403):
            with self.subTest(status=status):
                scenario = "auth-indices-gateway-empty-%d" % status
                tree, = self.render(scenario, 20, show_indices=True,
                                    state={"fake_now_et": "2026-11-02T21:00"})
                self.assertEqual(texts(tree), ["KEY REJECTED", "Wrong key or", "not subscribed", "rapidapi.com"])
                self.assertEqual(MockQuotes.requests[scenario], [("quotes", SYMBOLS[:5]), ("indices", INDEX_SYMBOLS)])
                self.assertEqual(self.last_cache_writes.count("cold"), 1)  # the fresh 200 spent the claim

    def test_quota_marquee_keeps_encoded_ticker_and_indices_pages(self):
        for plan in ("paid", "unknown"):
            for indices in (False, True):
                with self.subTest(plan=plan, indices=indices):
                    scenario = plan + "marquee" + str(indices)
                    tree, = self.render(scenario, 100, show_indices=indices)
                    self.assertTrue(displayed(tree))
                    marquee, = [n for n in walk(tree) if n["kind"] == "Marquee"]
                    self.assertEqual(texts(marquee), [UPGRADE_URL])
                    if indices:
                        self.assertEqual(texts(pages(tree)[0])[0], "DOW")
                        self.assertNotIn("no data", texts(pages(tree)[0]))
                    static_pages = len(pages(tree)) - 1
                    self.assertGreaterEqual(sum(d >= 1500 for d in self.last_durations), static_pages)
                    self.assertGreater(len(self.last_durations), static_pages)
                    self.assertLessEqual(sum(self.last_durations), 15000)
                    scroll = sum(d for d in self.last_durations if d < 1500)
                    self.assertGreaterEqual(scroll, 3000)
                    self.assertLessEqual(scroll, UPGRADE_SCROLL_MS)

    def test_failed_origin_quota_response_has_upgrade_marquee(self):
        tree, = self.render("originquota", 5)
        self.assertIn("FREE: 5/DAY USED", texts(tree))
        marquee, = [n for n in walk(tree) if n["kind"] == "Marquee"]
        self.assertEqual(texts(marquee), [UPGRADE_URL])
        self.assertTrue(displayed(tree))

    def test_indices_signed_points_and_ticker_prices(self):
        cases = (
            ("indicespoints", ["+245", "-38", "0.0", "+19", "+0.4"],
             ["#00d26a", "#ff3b30", "#9e9e9e", "#00d26a", "#00d26a"], ["+0.5%", "-0.7%", "0.0%", "+0.9%", "+2.2%"]),
            ("indicesfallback", ["+10", "-3.4", "0.0", "+4.4", "+0.4"],
             ["#00d26a", "#ff3b30", "#9e9e9e", "#00d26a", "#00d26a"], ["-0.5%"] * 5),
            ("indicesmissing", ["--"] * 5, ["#9e9e9e"] * 5, ["-0.5%"] * 5),
            ("indicesbadprice", ["--"] * 5, ["#9e9e9e"] * 5, ["-0.5%"] * 5),
            ("indicestiny", ["+9.2", "+<0.1", "-<0.1", "+19", "+0.4"],
             ["#00d26a", "#00d26a", "#ff3b30", "#00d26a", "#00d26a"], ["-0.5%"] * 5),
            ("indicesstale", ["-10"] * 5, ["#5a5a5a"] * 5, ["-0.5%"] * 5),
        )
        for scenario, points, colors, percentages in cases:
            with self.subTest(scenario=scenario):
                tree, = self.render(scenario, 5, show_indices=True)
                nodes = [n for n in walk(pages(tree)[0]) if n["kind"] == "Text"]
                self.assertEqual([n["props"]["content"] for n in nodes][1::3], points)
                self.assertEqual([n["props"]["color"] for n in nodes][1::3], colors)
                self.assertEqual([n["props"]["content"] for n in nodes][2::3], percentages)
                expected_colors = colors if scenario in ("indicespoints", "indicesstale") else ["#ff3b30"] * 5
                self.assertEqual([n["props"]["color"] for n in nodes][2::3], expected_colors)
                ticker = next(n for n in walk(tree) if n["kind"] == "Row" and texts(n)[:1] == [SYMBOLS[0]])
                self.assertEqual(texts(ticker)[1], "100")
                self.assertNotIn("47821", texts(pages(tree)[0]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
