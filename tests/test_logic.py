#!/usr/bin/env python3
"""Offline checks of the app source using small Pixlet module doubles.

These exercise Python-compatible Starlark logic, not Pixlet's interpreter/encoder.
The real Pixlet integration suite remains the authority for those interfaces.
"""
import builtins
from datetime import datetime, timedelta
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
INDEX_SYMBOLS = ["DJI", "SPX", "NDX", "RUT", "VIX"]
SYMBOLS = ["T" + chr(65 + i // 26) + chr(65 + i % 26) for i in range(100)]
ET = ZoneInfo("America/New_York")


class ClockValue:
    def __init__(self, value):
        self.value = value

    @property
    def unix(self):
        return int(self.value.timestamp())

    def __getattr__(self, name):
        return getattr(self.value, name)

    def in_location(self, location):
        return ClockValue(self.value.astimezone(ZoneInfo(location)))

    def format(self, layout):
        formats = {"Mon": "%a", "2006-01-02": "%Y-%m-%d", "15:04": "%H:%M",
                   "Mon 15:04": "%a %H:%M", "2006-01-02 15:04": "%Y-%m-%d %H:%M"}
        return self.value.strftime(formats[layout])


class Clock:
    def __init__(self):
        self.elapsed = 0.0
        self.base = datetime(2026, 11, 2, 10, 15, tzinfo=ET)

    def now(self):
        return ClockValue(self.base + timedelta(seconds=self.elapsed))

    def parse_time(self, value, format=None, location="America/New_York"):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return ClockValue(parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo(location)))

    def time(self, year, month, day, hour=0, minute=0, location="America/New_York"):
        return ClockValue(datetime(year, month, 1, tzinfo=ZoneInfo(location)) +
                          timedelta(days=day - 1, hours=hour, minutes=minute))

    def from_timestamp(self, value):
        return ClockValue(datetime.fromtimestamp(value, ET))


class Cache:
    def __init__(self):
        self.values = {}
        self.writes = []

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ttl_seconds):
        self.values[key] = value
        self.writes.append((key, value))

    def last(self):
        return next((json.loads(v) for k, v in self.values.items() if k.startswith("last:")), {})


class Config(dict):
    def str(self, key, default=""):
        return self.get(key, default)

    def bool(self, key, default=False):
        return self.get(key, default)


def decode(value, default=None):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def quote(symbol):
    return {"price": 100 + SYMBOLS.index(symbol), "change_percentage": 1.0,
            "previous_close": 99, "market_status": "open", "quote_status": "delayed",
            "session_date": "2026-11-02", "as_of": "2026-11-02T10:00:00-05:00"}


def index_quote(symbol):
    return {"price": {"DJI": 42000, "SPX": 5700, "NDX": 20000, "RUT": 2200, "VIX": 18.4}[symbol],
            "change_percentage": -0.5, "change": -10, "previous_close": 20010,
            "name": symbol, "data_updated": True, "is_stale": False,
            "as_of": "2026-11-02T10:00:00-05:00"}


class HTTP:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.requests = []
        self.request_headers = []
        self.urls = []
        self.ttls = []
        self.request_times = []
        self.limited = 0
        self.delay = 0
        self.limit = 100000
        self.quota = False
        self.fail_at = None
        self.error_symbols = set()
        self.emit_limit = True
        self.emit_headers = True
        self.quote_overrides = {}
        self.before_request = None
        self.answers = {}
        self.index_status = 200
        self.index_body = None
        self.index_overrides = {}
        self.burst = False
        self.remaining = None
        self.error_body = None
        self.status_override = None
        self.body_overrides = {}
        self.header_overrides = {}

    def get(self, url, params, headers, ttl_seconds):
        parsed = urlsplit(url)
        assert ((parsed.scheme == "http" and parsed.hostname == "127.0.0.1") or
                (parsed.scheme == "https" and parsed.netloc == "us-stock-etf-prices.p.rapidapi.com"))
        self.urls.append(url)
        self.ttls.append(ttl_seconds)
        endpoint = parsed.path.rsplit("/", 1)[-1]
        assert endpoint in ("quotes", "indices")
        symbols = params["symbols"].split(",")
        cache_key = (endpoint, tuple(symbols))
        if cache_key in self.answers:
            answer = self.answers[cache_key]
            return SimpleNamespace(status_code=200, headers=dict(answer.headers, **{"Tidbyt-Cache-Status": "HIT"}),
                                   body=answer.body)
        if self.before_request:
            self.before_request(symbols)
        self.requests.append((endpoint, symbols))
        self.request_headers.append(headers)
        if endpoint == "quotes":
            self.calls.append(symbols)
        now = self.clock.elapsed
        self.request_times = [t for t in self.request_times if now - t < 1]
        self.request_times.append(now)
        limited = len(self.request_times) > 5
        self.limited += limited
        self.clock.elapsed += self.delay
        quota = self.quota or len(self.requests) == self.fail_at
        status = 502 if self.error_symbols.intersection(symbols) else 429 if limited or quota or self.burst else 200
        if endpoint == "indices" and status == 200:
            status = self.index_status
        if self.status_override is not None:
            status = self.status_override
        body = {"message": "quota exhausted" if quota else "rate limit"} if status == 429 else {
            "data": {s: dict(index_quote(s) if endpoint == "indices" else quote(s),
                             **(self.index_overrides if endpoint == "indices" else self.quote_overrides)) for s in symbols}}
        if endpoint == "indices" and self.index_body is not None:
            body = self.index_body
        if status == 429 and self.error_body is not None:
            body = self.error_body
        if self.body_overrides:
            body.update(self.body_overrides)
        answer = SimpleNamespace(status_code=status, headers={
            "X-Ratelimit-Requests-Limit": str(self.limit),
            "X-Ratelimit-Requests-Remaining": "0" if quota else "4" if self.limit < 1000 else "90000",
            "X-Ratelimit-Requests-Reset": "3600"}, body=lambda: json.dumps(body))
        if self.remaining is not None:
            answer.headers["X-Ratelimit-Requests-Remaining"] = str(self.remaining)
        if not self.emit_limit:
            answer.headers.pop("X-Ratelimit-Requests-Limit")
        if not self.emit_headers:
            answer.headers.clear()
        answer.headers.update(self.header_overrides)
        if status == 200:
            self.answers[cache_key] = answer
        return answer


class Runtime:
    def __init__(self, dwell=15000):
        self.clock, self.cache, self.lines = Clock(), Cache(), []
        self.http = HTTP(self.clock)
        widgets = {}
        for kind in ("Root", "Row", "Box", "Text", "Column", "Padding", "Animation", "Marquee", "Sequence"):
            def widget(*args, _kind=kind, **kwargs):
                if args:
                    kwargs["content"] = args[0]
                children = kwargs.pop("children", [])
                if "child" in kwargs:
                    children = [kwargs.pop("child")]
                return {"kind": _kind, "props": kwargs, "children": children}
            widgets[kind] = widget
        self.env = {"cache": self.cache, "http": self.http, "time": self.clock,
                    "canvas": SimpleNamespace(max_duration_ms=lambda: dwell),
                    "render": SimpleNamespace(**widgets),
                    "schema": SimpleNamespace(**{
                        kind: (lambda _kind=kind, **kwargs: dict(kind=_kind, **kwargs))
                        for kind in ("Schema", "Text", "Toggle")}),
                    "hash": SimpleNamespace(sha256=lambda s: hashlib.sha256(s.encode()).hexdigest()),
                    "json": SimpleNamespace(encode=json.dumps, decode=decode),
                    "re": SimpleNamespace(match=lambda pattern, s: list(m.groups(default="")) if (m := re.match(pattern, s)) else []),
                    "humanize": SimpleNamespace(float=lambda fmt, n: format(n, ".%df" % len(fmt.split(".")[1]))),
                    "type": lambda v: "string" if isinstance(v, str) else builtins.type(v).__name__,
                    "print": lambda *args: self.lines.append(" ".join(str(a) for a in args))}
        source = (ROOT / "stock_watchlist.star").read_text()
        exec(re.sub(r"^load\(.*\)\n", "", source, flags=re.M), self.env)

    def render(self, **params):
        config = Config(rapidapi_key="synthetic-test", symbols=",".join(SYMBOLS),
                        fake_now_et="2026-11-02T10:15", api_base="http://127.0.0.1:12345/mock",
                        show_indices=False)
        config.update(params)
        if config.get("show_indices") is None:
            config.pop("show_indices")
        if config.get("rapidapi_key") is None:
            config.pop("rapidapi_key")
        return self.env["main"](config)

    def state(self):
        states = [re.fullmatch(r"STATE page_offset=(\d+) plan_limit=(\d+)", s) for s in self.lines]
        states = [s for s in states if s]
        if not states:
            raise AssertionError("render did not print STATE")
        return dict(zip(("page_offset", "plan_limit"), states[-1].groups()))


def walk(tree):
    yield tree
    for child in tree["children"]:
        yield from walk(child)


def shown(tree):
    return [n["props"]["content"] for n in walk(tree)
            if n["kind"] == "Text" and n["props"]["content"] in SYMBOLS]


def texts(tree):
    return [n["props"]["content"] for n in walk(tree) if n["kind"] == "Text"]


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


class LogicTests(unittest.TestCase):
    def test_request_cap_and_rotation(self):
        runtime = Runtime()
        for batch in range(4):
            # Consecutive scheduled renders; the mock enforces 5 requests in a rolling second.
            runtime.clock.elapsed += 600
            before = len(runtime.http.calls)
            runtime.render()
            self.assertEqual(len(runtime.http.calls) - before, 5)
            self.assertEqual(runtime.state()["page_offset"], str(((batch + 1) * 25) % 100))
        self.assertEqual(runtime.http.limited, 0)
        self.assertEqual([s for c in runtime.http.calls for s in c], SYMBOLS)

    def test_partial_quotes_saved_before_next_request(self):
        runtime = Runtime()
        runtime.http.delay = 1.6
        def check_saved(symbols):
            previous = [s for c in runtime.http.calls for s in c]
            self.assertTrue(set(previous) <= set(runtime.cache.last()))
        runtime.http.before_request = check_saved
        runtime.render()
        self.assertEqual(runtime.clock.elapsed, 8)
        self.assertEqual(set(runtime.cache.last()) - {"_old"}, set(SYMBOLS[:25]))
        self.assertEqual(len([k for k, _ in runtime.cache.writes if k.startswith("last:")]), 5)

    def test_elapsed_budget_reserves_time_for_inflight_request(self):
        runtime = Runtime()
        runtime.http.delay = 4.9  # just inside Pixlet's 5 s HTTP timeout
        runtime.render()
        self.assertLessEqual(runtime.clock.elapsed, 15)
        self.assertEqual(len(runtime.http.calls), 3)
        self.assertEqual(set(runtime.cache.last()) - {"_old"}, set(SYMBOLS[:15]))
        self.assertEqual(runtime.state()["page_offset"], "15")

    def test_elapsed_budget_never_displays_unattempted_cached_rows(self):
        runtime = Runtime()
        for _ in range(4):
            runtime.clock.elapsed += 600
            runtime.render()
        runtime.http.answers.clear()
        runtime.http.delay = 4.9
        runtime.clock.elapsed += 600
        before = len(runtime.http.calls)
        tree = runtime.render()
        self.assertEqual(len(runtime.http.calls) - before, 3)
        self.assertEqual(shown(tree), SYMBOLS[:15])
        self.assertEqual(runtime.state()["page_offset"], "15")

    def test_failed_requests_keep_and_dim_cached_prices(self):
        runtime = Runtime()
        for _ in range(4):
            runtime.clock.elapsed += 600
            runtime.render()
        runtime.http.answers.clear()
        runtime.clock.elapsed += 600
        runtime.http.error_symbols = set(SYMBOLS[:25])
        tree = runtime.render()
        last = runtime.cache.last()
        self.assertTrue(all(last[s]["price"] == quote(s)["price"] for s in SYMBOLS))
        self.assertEqual(shown(tree), SYMBOLS[:25])
        self.assertNotIn("no data", [n["props"].get("content") for n in walk(tree)])
        dimmed = [n["props"]["content"] for n in walk(tree)
                  if n["kind"] == "Text" and n["props"].get("color") == "#5a5a5a"]
        self.assertTrue(dimmed)

    def test_explicit_state_overrides_cache_and_crosses_fresh_runtimes(self):
        for count in (50, 100):
            with self.subTest(count=count):
                state, stream = dict(page_offset="0", plan_limit="0"), []
                for _ in range(count // 25):
                    runtime = Runtime()
                    tokens = [(s, True) for s in SYMBOLS[:count]]
                    key = "page:" + runtime.env["hash"].sha256("synthetic-test" + json.dumps(tokens))
                    runtime.cache.values[key] = "19"
                    tree = runtime.render(symbols=",".join(SYMBOLS[:count]), **state)
                    fetched = [s for c in runtime.http.calls for s in c]
                    self.assertEqual(shown(tree), fetched)
                    self.assertEqual(len(fetched), 25)
                    self.assertNotIn("no data", [n["props"].get("content") for n in walk(tree)])
                    stream.extend(shown(tree))
                    state = runtime.state()
                self.assertEqual(stream, SYMBOLS[:count])
                self.assertEqual(state["page_offset"], "0")

    def test_small_dwell_fetches_only_display_capacity_and_resumes(self):
        for dwell, capacity in ((1500, 4), (3000, 8), (4500, 12), (7500, 20)):
            with self.subTest(dwell=dwell):
                runtime = Runtime(dwell)
                tree = runtime.render(page_offset="17", plan_limit="100000")
                self.assertEqual(shown(tree), SYMBOLS[17:17 + capacity])
                self.assertEqual([s for c in runtime.http.calls for s in c], shown(tree))
                self.assertEqual(len(runtime.http.calls), (capacity + 4) // 5)
                self.assertEqual(runtime.state()["page_offset"], str(17 + capacity))

    def test_new_status_line_reduces_short_dwell_requests(self):
        runtime = Runtime(4500)
        runtime.http.quote_overrides = {"quote_status": "closed", "market_status": "closed"}
        tree = runtime.render(plan_limit="100000")
        self.assertEqual(shown(tree), SYMBOLS[:12])
        self.assertEqual(runtime.http.calls, [SYMBOLS[:5], SYMBOLS[5:10], SYMBOLS[10:12]])
        self.assertEqual(runtime.state()["page_offset"], "12")
        self.assertFalse(any(t.startswith("CLOSED") for t in texts(tree)))
        self.assertFalse(any(re.fullmatch(r"\d+-\d+/\d+", t) for t in texts(tree)))

    def test_closed_market_fills_every_window_with_persistent_cache(self):
        runtime = Runtime()
        runtime.http.quote_overrides = {"quote_status": "closed", "market_status": "closed"}
        self.assertEqual(runtime.cache.values, {})
        stream = []
        for batch in range(8):
            runtime.clock.elapsed += 600
            tree = runtime.render()
            expected = SYMBOLS[(batch % 4) * 25:(batch % 4 + 1) * 25]
            self.assertEqual(shown(tree), expected)
            content = [n["props"].get("content") for n in walk(tree)]
            self.assertNotIn("no data", content)
            for symbol in expected:
                self.assertIn(runtime.env["fmt_price"](float(quote(symbol)["price"])), content)
            stream.extend(shown(tree))
        self.assertEqual(stream, SYMBOLS * 2)
        self.assertEqual([s for c in runtime.http.calls for s in c], SYMBOLS)
        self.assertTrue(any(k.startswith("dormant:") for k in runtime.cache.values))

    def test_new_status_at_minimum_dwell_never_refetches_hidden_rows(self):
        for overrides in ({"quote_status": "closed", "market_status": "closed"},
                          {"quote_status": "stale", "is_stale": True}):
            with self.subTest(overrides=overrides):
                runtime = Runtime(1500)
                runtime.http.quote_overrides = overrides
                stream = []
                for batch in range(2):
                    runtime.clock.elapsed += 600
                    before = len(runtime.http.calls)
                    tree = runtime.render(plan_limit="100000")
                    fetched = [s for c in runtime.http.calls[before:] for s in c]
                    self.assertEqual(fetched, shown(tree))
                    self.assertEqual(shown(tree), SYMBOLS[batch * 4:(batch + 1) * 4])
                    self.assertEqual(runtime.state()["page_offset"], str((batch + 1) * 4))
                    stream.extend(shown(tree))
                self.assertEqual(stream, SYMBOLS[:8])
                self.assertEqual([s for c in runtime.http.calls for s in c], stream)

    def test_end_of_list_wraps_without_fetching_unshown_rows(self):
        runtime = Runtime()
        tree = runtime.render(page_offset="98", plan_limit="100000", fetch_cursor="8")
        self.assertEqual(shown(tree), SYMBOLS[98:])
        self.assertEqual(runtime.http.calls, [SYMBOLS[98:]])
        self.assertEqual(runtime.state()["page_offset"], "0")

    def test_paid_window_of_invalid_rows_needs_no_request_and_advances(self):
        runtime = Runtime(1500)
        tree = runtime.render(symbols="123,!,?,BAD!," + ",".join(SYMBOLS[:5]), plan_limit="100000")
        self.assertEqual(runtime.http.calls, [])
        self.assertEqual([n["props"].get("content") for n in walk(tree)].count("invalid"), 4)
        self.assertEqual(runtime.state()["page_offset"], "4")

    def test_notices_keep_quote_rows_paid_paging_and_free_reset(self):
        for dwell in (1500, 3000):
            for free in (False, True):
                with self.subTest(dwell=dwell, free=free):
                    runtime = Runtime(dwell)
                    runtime.http.limit = 5 if free else 100000
                    runtime.render()  # learn the plan and save quotes
                    runtime.cache.values["pause:" + hashlib.sha256(b"synthetic-test").hexdigest()] = str(runtime.clock.now().unix + 3600)
                    first = runtime.render(page_offset="0")
                    state = runtime.state()
                    self.assertTrue(shown(first))
                    self.assertEqual(state["page_offset"] == "0", free)
                    second = runtime.render(**state)
                    self.assertTrue(shown(second))
                    self.assertEqual(shown(first)[0] == shown(second)[0], free)
                    for tree in (first, second):
                        animation = next((n for n in walk(tree) if n["kind"] == "Animation"), None)
                        if animation:
                            self.assertLessEqual(len(animation["children"]) * tree["props"]["delay"], dwell)

    def test_free_first_chunk_ignores_both_old_offsets_and_quota_still_pauses(self):
        runtime = Runtime()
        runtime.http.limit = 5
        runtime.cache.values["lim:" + hashlib.sha256(b"synthetic-test").hexdigest()] = "5"
        runtime.render(page_offset="60", fetch_cursor="12")
        self.assertEqual(runtime.http.calls, [SYMBOLS[:5]])
        self.assertEqual(runtime.state()["page_offset"], "0")
        runtime.http.quota = True
        runtime.http.answers.clear()
        runtime.clock.elapsed += 600
        runtime.render(fake_now_et="2026-11-02T11:50")
        count = len(runtime.http.calls)
        runtime.render(fake_now_et="2026-11-02T11:50")
        self.assertEqual(len(runtime.http.calls), count)
        self.assertTrue(any(k.startswith("pause:") for k in runtime.cache.values))

    def test_unknown_plan_preserves_offset_until_a_limit_is_learned(self):
        for limit in (5, 100000):
            runtime = Runtime()
            runtime.http.limit = limit
            tree = runtime.render(page_offset="60", fetch_cursor="12")
            self.assertEqual(runtime.http.calls[0], SYMBOLS[60:65])
            self.assertEqual(shown(tree), SYMBOLS[:5] if limit == 5 else SYMBOLS[60:85])
            self.assertEqual(runtime.state()["page_offset"], "0" if limit == 5 else "85")
            self.assertEqual(runtime.state()["plan_limit"], str(limit))

    def test_missing_plan_header_fetches_full_window_and_continues_paging(self):
        runtime = Runtime()
        runtime.http.emit_headers = False
        tree = runtime.render(page_offset="60")
        self.assertEqual([s for c in runtime.http.calls for s in c], SYMBOLS[60:85])
        self.assertEqual(shown(tree), SYMBOLS[60:85])
        self.assertEqual(runtime.state(), {"page_offset": "85", "plan_limit": "0"})
        self.assertFalse(any("PRO" in t or "FREE" in t for t in texts(tree)))
        runtime.clock.elapsed += 600
        tree = runtime.render()
        self.assertEqual(shown(tree), SYMBOLS[85:])
        self.assertEqual(runtime.state()["page_offset"], "0")

    def test_cached_free_limit_overrides_an_older_paid_hint(self):
        runtime = Runtime()
        runtime.http.limit = 5
        runtime.cache.values["lim:" + hashlib.sha256(b"synthetic-test").hexdigest()] = "5"
        runtime.render(page_offset="60", plan_limit="100000")
        self.assertEqual(runtime.http.calls, [SYMBOLS[:5]])
        self.assertEqual(runtime.state()["page_offset"], "0")

    def test_changed_key_preserves_offset_before_learning_its_plan(self):
        runtime = Runtime()
        runtime.render()
        runtime.clock.elapsed += 600
        runtime.http.answers.clear()
        runtime.http.limit = 5
        runtime.render(rapidapi_key="changed-key", page_offset="25")
        self.assertEqual(runtime.http.calls[-1], SYMBOLS[25:30])
        self.assertEqual(len(runtime.http.calls), 6)
        self.assertEqual(runtime.state()["page_offset"], "0")

    def test_state_line_on_early_returns(self):
        for params in ({"rapidapi_key": ""}, {"symbols": "123"}, {}):
            runtime = Runtime()
            runtime.render(**params)
            self.assertEqual(sum(s.startswith("STATE ") for s in runtime.lines), 1)
            runtime.state()

    def test_no_key_setup_never_calls_or_changes_cached_state(self):
        for key in ("", " \n\t", None):
            for explicit in (False, True):
                with self.subTest(key=key, explicit=explicit):
                    runtime = Runtime()
                    tokens = [(s, True) for s in SYMBOLS[:8]]
                    identity = hashlib.sha256(json.dumps(tokens).encode()).hexdigest()
                    empty_key = hashlib.sha256(b"").hexdigest()
                    runtime.cache.values.update({"page:" + identity: "7", "lim:" + empty_key: "100000",
                                                 "last-indices:" + empty_key: json.dumps({"DJI": index_quote("DJI")})})
                    before = dict(runtime.cache.values)
                    params = {"page_offset": "23"} if explicit else {}
                    tree = runtime.render(rapidapi_key=key, symbols=",".join(SYMBOLS[:8]),
                                          show_indices=True, **params)
                    self.assertEqual(runtime.http.requests, [])
                    self.assertEqual(runtime.cache.writes, [])
                    self.assertEqual(runtime.cache.values, before)
                    self.assertEqual(runtime.state(), {"page_offset": "23" if explicit else "7", "plan_limit": "100000"})
                    self.assertEqual(sum(s.startswith("STATE ") for s in runtime.lines), 1)
                    self.assertIn("ADD RAPIDAPI KEY", texts(tree))
                    self.assertIn("FREE KEY WORKS", texts(tree))
                    self.assertIn("FIRST 5 TICKERS", texts(tree))
                    marquee, = [n for n in walk(tree) if n["kind"] == "Marquee"]
                    self.assertEqual(texts(marquee), [runtime.env["UPGRADE_URL"]])
                    self.assertEqual(marquee["props"]["width"], 64)

    def test_present_key_still_fetches_quotes_and_indices_with_headers(self):
        runtime = Runtime()
        tree = runtime.render(rapidapi_key=" synthetic-test \n", show_indices=True)
        self.assertEqual(len(runtime.http.requests), 5)
        self.assertIn(("indices", INDEX_SYMBOLS), runtime.http.requests)
        self.assertTrue(all(h == {"X-RapidAPI-Key": "synthetic-test", "X-RapidAPI-Host": runtime.env["API_HOST"]}
                            for h in runtime.http.request_headers))
        self.assertEqual(shown(tree), SYMBOLS[:20])
        self.assertNotIn(runtime.env["UPGRADE_URL"], texts(tree))

    def test_quota_marquee_reserves_scroll_time_and_keeps_quote_and_indices_pages(self):
        for indices in (False, True):
            for plan in (0, 5, 100000):
                for dwell in (1500, 3000, 4500, 8000, 15000):
                    with self.subTest(indices=indices, plan=plan, dwell=dwell):
                        runtime = Runtime(dwell)
                        runtime.http.emit_limit = plan != 0
                        runtime.http.limit = plan or 100000
                        # Leave fresh ticker/indices data before quota is exhausted, unless BASIC.
                        tight = dwell < 6500 + (1500 if indices else 0)
                        runtime.http.fail_at = 1 if plan == 5 or tight else 3 if indices else 2
                        tree = runtime.render(show_indices=indices, plan_limit=str(plan))
                        self.assertTrue(shown(tree))
                        self.assertTrue(any("USED" in t for t in texts(tree)))
                        if indices:
                            self.assertEqual(texts(pages(tree)[0])[0], "DOW")
                        marquees = [n for n in walk(tree) if n["kind"] == "Marquee"]
                        if tight:
                            self.assertEqual(marquees, [])
                            self.assertNotIn(runtime.env["UPGRADE_URL"], texts(tree))
                        else:
                            self.assertEqual(len(marquees), 1)
                            self.assertEqual(texts(marquees[0]), [runtime.env["UPGRADE_URL"]])
                            self.assertEqual(marquees[0]["children"][0]["props"]["font"], "tom-thumb")
                        if len(pages(tree)) > 1:
                            static_pages = len(pages(tree)) - len(marquees)
                            minimum = min(1500, dwell // 2) if indices else 1500
                            if marquees:
                                self.assertEqual(tree["props"]["delay"], runtime.env["UPGRADE_FRAME_MS"])
                                self.assertLessEqual(static_pages * minimum + len(marquees) * runtime.env["UPGRADE_SCROLL_MS"], dwell)
                            else:
                                delay = tree["props"]["delay"]
                                self.assertGreaterEqual(delay, minimum)
                                self.assertLessEqual(static_pages * delay, dwell)

    def test_indices_middle_column_uses_signed_points_and_tickers_keep_prices(self):
        runtime = Runtime()
        runtime.http.index_body = {"data": {
            "DJI": {"price": 47821, "change": 245.2, "change_percentage": 0.5},
            "SPX": {"price": 5700, "change": -38.4, "change_percentage": -0.7},
            "NDX": {"price": 20000, "change": 0, "change_percentage": 0},
            "RUT": {"price": 2200, "change": 18.6, "change_percentage": 0.9},
            "VIX": {"price": 18.4, "change": 0.4, "change_percentage": 2.2}}}
        runtime.http.quote_overrides = {"change": -99}
        tree = runtime.render(show_indices=True)
        nodes = [n for n in walk(pages(tree)[0]) if n["kind"] == "Text"]
        self.assertEqual([n["props"]["content"] for n in nodes],
                         ["DOW", "+245", "+0.5%", "S&P", "-38", "-0.7%", "NDX", "0.0", "0.0%",
                          "RUT", "+19", "+0.9%", "VIX", "+0.4", "+2.2%"])
        self.assertEqual([n["props"]["color"] for n in nodes][1::3],
                         [runtime.env["UP"], runtime.env["DOWN"], runtime.env["FLAT"], runtime.env["UP"], runtime.env["UP"]])
        self.assertEqual([n["props"]["color"] for n in nodes][2::3],
                         [runtime.env["UP"], runtime.env["DOWN"], runtime.env["FLAT"], runtime.env["UP"], runtime.env["UP"]])
        ticker = next(n for n in walk(tree) if n["kind"] == "Row" and texts(n)[:1] == [SYMBOLS[0]])
        self.assertEqual(texts(ticker)[1], "100")
        self.assertNotIn("47821", texts(pages(tree)[0]))

    def test_indices_change_fallback_and_missing_values_keep_percentages(self):
        cases = [
            ({"previous_close": 99}, "+1.0", "UP"),
            ({"change": None, "previous_close": 103.4}, "-3.4", "DOWN"),
            ({"change": "bad", "previous_close": 100}, "0.0", "FLAT"),
            ({}, "--", "FLAT"),
            ({"change": "bad", "previous_close": "bad"}, "--", "FLAT"),
            ({"change": 9.2}, "+9.2", "UP"),
            ({"change": -9.2}, "-9.2", "DOWN"),
            ({"change": 0.4}, "+0.4", "UP"),
            ({"change": 0.001}, "+<0.1", "UP"),
            ({"change": -0.001}, "-<0.1", "DOWN"),
            ({"price": "bad", "previous_close": 99}, "--", "FLAT"),
            ({"price": None, "change": 38}, "+38", "UP"),
        ]
        for fields, expected, color in cases:
            with self.subTest(fields=fields):
                runtime = Runtime()
                runtime.http.index_body = {"data": {s: dict({"price": 100, "change_percentage": 0.5}, **fields)
                                                   for s in INDEX_SYMBOLS}}
                tree = runtime.render(show_indices=True)
                nodes = [n for n in walk(pages(tree)[0]) if n["kind"] == "Text"]
                self.assertEqual([n["props"]["content"] for n in nodes][1::3], [expected] * 5)
                self.assertEqual([n["props"]["color"] for n in nodes][1::3], [runtime.env[color]] * 5)
                self.assertEqual([n["props"]["content"] for n in nodes][2::3], ["+0.5%"] * 5)

    def test_stale_indices_dim_both_point_change_and_percentage(self):
        runtime = Runtime()
        runtime.http.index_overrides = {"is_stale": True, "change": 245, "change_percentage": 0.5}
        tree = runtime.render(show_indices=True)
        nodes = [n for n in walk(pages(tree)[0]) if n["kind"] == "Text"]
        self.assertEqual([n["props"]["content"] for n in nodes][1::3], ["+245"] * 5)
        self.assertEqual([n["props"]["color"] for n in nodes][1::3], [runtime.env["DIM"]] * 5)
        self.assertEqual([n["props"]["color"] for n in nodes][2::3], [runtime.env["DIM"]] * 5)

    def test_quota_after_partial_batch_keeps_quotes_and_stops_following_render(self):
        runtime = Runtime()
        runtime.http.fail_at = 3
        tree = runtime.render()
        self.assertEqual(len(runtime.http.calls), 3)
        self.assertEqual(set(runtime.cache.last()) - {"_old"}, set(SYMBOLS[:10]))
        self.assertIn("QUOTA USED UP", [n["props"].get("content") for n in walk(tree)])
        runtime.render()
        self.assertEqual(len(runtime.http.calls), 3)

    def test_newly_discovered_free_quota_resets_offset(self):
        runtime = Runtime()
        runtime.http.limit, runtime.http.quota = 5, True
        runtime.render()
        self.assertEqual(runtime.state()["page_offset"], "0")

    def test_successful_basic_has_no_upgrade_notice(self):
        runtime = Runtime()
        runtime.http.limit = 5
        tree = runtime.render(plan_limit="5")
        self.assertEqual(runtime.http.calls, [SYMBOLS[:5]])
        self.assertEqual(shown(tree), SYMBOLS[:5])
        self.assertFalse(any("PRO" in t or "NEED" in t for t in texts(tree)))
        self.assertNotIn(runtime.env["UPGRADE_URL"], texts(tree))

    def test_basic_same_slot_last_good_reuse_keeps_fresh_quote_colors(self):
        runtime = Runtime()
        runtime.http.limit = 5
        first = runtime.render(plan_limit="5")
        self.assertEqual(runtime.http.calls, [SYMBOLS[:5]])
        runtime.clock.elapsed += 60
        reused = runtime.render(fake_now_et="2026-11-02T10:16")
        self.assertEqual(runtime.http.ttls, [7200])  # schedule skips HTTP entirely
        for tree in (first, reused):
            self.assertEqual(shown(tree), SYMBOLS[:5])
            rows = [n for n in walk(tree) if n["kind"] == "Row" and len(n["children"]) == 3]
            nodes = [n for row in rows for n in walk(row) if n["kind"] == "Text"]
            self.assertEqual([n["props"]["color"] for n in nodes][1::3], ["#ffffff"] * 5)
            self.assertEqual([n["props"]["color"] for n in nodes][2::3], ["#00d26a"] * 5)

    def test_basic_same_slot_last_good_dims_only_stale_or_old_rows(self):
        runtime = Runtime()
        runtime.http.limit = 5
        runtime.render(plan_limit="5")
        last = runtime.cache.last()
        last[SYMBOLS[0]]["quote_status"] = "stale"
        last[SYMBOLS[1]]["is_stale"] = True
        last["_old"] = [SYMBOLS[2]]
        key = next(k for k in runtime.cache.values if k.startswith("last:"))
        runtime.cache.values[key] = json.dumps(last)
        runtime.clock.elapsed += 60
        tree = runtime.render(fake_now_et="2026-11-02T10:16")
        self.assertEqual(runtime.http.ttls, [7200])
        self.assertEqual(shown(tree), SYMBOLS[:5])
        rows = [n for n in walk(tree) if n["kind"] == "Row" and len(n["children"]) == 3]
        nodes = [n for row in rows for n in walk(row) if n["kind"] == "Text"]
        self.assertEqual([n["props"]["color"] for n in nodes][1::3],
                         ["#5a5a5a"] * 3 + ["#ffffff"] * 2)
        self.assertEqual([n["props"]["color"] for n in nodes][2::3],
                         ["#5a5a5a"] * 3 + ["#00d26a"] * 2)

    def test_basic_quota_pause_still_dims_fresh_last_good_rows(self):
        runtime = Runtime()
        runtime.http.limit = 5
        runtime.render(plan_limit="5")
        key = "pause:" + hashlib.sha256(b"synthetic-test").hexdigest()
        runtime.cache.values[key] = str(runtime.clock.now().unix + 3600)
        tree = runtime.render(fake_now_et="2026-11-02T10:16")
        self.assertEqual(runtime.http.ttls, [7200])
        self.assertEqual([s for page in pages(tree) for s in shown(page)], SYMBOLS[:5])
        rows = [n for page in pages(tree) for n in walk(page)
                if n["kind"] == "Row" and len(n["children"]) == 3]
        nodes = [n for row in rows for n in walk(row) if n["kind"] == "Text"]
        self.assertEqual([n["props"]["color"] for n in nodes][1::3], ["#5a5a5a"] * 5)
        self.assertEqual([n["props"]["color"] for n in nodes][2::3], ["#5a5a5a"] * 5)

    def test_successful_http_cache_hit_keeps_quote_colors(self):
        runtime = Runtime()
        symbols = ",".join(SYMBOLS[:5])
        runtime.render(symbols=symbols)
        runtime.clock.elapsed += 60
        tree = runtime.render(symbols=symbols, fake_now_et="2026-11-02T10:16")
        self.assertEqual(runtime.http.ttls, [540, 540])
        self.assertEqual(runtime.http.calls, [SYMBOLS[:5]])  # second request hits HTTP cache
        self.assertEqual(shown(tree), SYMBOLS[:5])
        rows = [n for n in walk(tree) if n["kind"] == "Row" and len(n["children"]) == 3]
        nodes = [n for row in rows for n in walk(row) if n["kind"] == "Text"]
        self.assertEqual([n["props"]["color"] for n in nodes][1::3], ["#ffffff"] * 5)
        self.assertEqual([n["props"]["color"] for n in nodes][2::3], ["#00d26a"] * 5)

    def test_unknown_later_basic_header_uses_first_chunk_on_next_slot(self):
        runtime = Runtime()
        runtime.http.emit_limit = False
        runtime.render()
        runtime.http.emit_limit, runtime.http.limit = True, 5
        runtime.clock.elapsed += 600
        runtime.render()
        self.assertEqual(runtime.state()["page_offset"], "0")
        self.assertEqual(runtime.state()["plan_limit"], "5")
        before = len(runtime.http.calls)
        runtime.clock.elapsed += 600
        runtime.http.answers.clear()
        tree = runtime.render(fake_now_et="2026-11-02T11:50")
        self.assertEqual(runtime.http.calls[before:], [SYMBOLS[:5]])
        self.assertEqual(shown(tree), SYMBOLS[:5])
        self.assertFalse(any("PRO" in t for t in texts(tree)))

    def test_unknown_quota_fallback_and_burst_are_distinct(self):
        for quota in (False, True):
            with self.subTest(quota=quota):
                runtime = Runtime()
                runtime.http.emit_limit = False
                runtime.http.quota, runtime.http.burst = quota, not quota
                tree = runtime.render()
                content = texts(tree)
                self.assertEqual(any(k.startswith("pause:") for k in runtime.cache.values), quota)
                if quota:
                    self.assertTrue(any("USED" in t for t in content))
                    self.assertIn(runtime.env["UPGRADE_URL"], content)
                    self.assertEqual(runtime.state()["page_offset"], "0")
                else:
                    self.assertFalse(any("PRO" in t or "FREE" in t or "QUOTA" in t for t in content))
                    self.assertNotIn(runtime.env["UPGRADE_URL"], content)
                    self.assertEqual(runtime.state()["plan_limit"], "0")
                runtime.http.burst = runtime.http.quota = False
                runtime.clock.elapsed += 600
                runtime.render(fake_now_et="2026-11-03T09:50", page_offset="25")
                self.assertEqual(len(runtime.http.calls), 2 if quota else 6)
                self.assertEqual(runtime.http.calls[1], SYMBOLS[:5] if quota else SYMBOLS[25:30])

    def test_indices_default_on_order_and_shared_request_cap(self):
        for toggle in (True, None):
            with self.subTest(toggle=toggle):
                runtime = Runtime()
                tree = runtime.render(show_indices=toggle)
                page = pages(tree)[0]
                self.assertEqual(texts(page)[::3], ["DOW", "S&P", "NDX", "RUT", "VIX"])
                self.assertEqual(page["props"]["pad"], (0, 1, 0, 0))
                rows = page["children"][0]["children"]
                self.assertEqual(len(rows), 5)
                for row in rows:
                    self.assertEqual([cell["props"]["width"] for cell in row["children"]], [20, 20, 24])
                    self.assertEqual([cell["props"]["height"] for cell in row["children"]], [6, 6, 6])
                self.assertLessEqual(1 + len(rows) * 6, 32)
                self.assertEqual(len(runtime.http.requests), 5)
                self.assertEqual(len(runtime.http.calls), 4)
                self.assertEqual([s for c in runtime.http.calls for s in c], SYMBOLS[:20])
                self.assertEqual(shown(tree), SYMBOLS[:20])
                self.assertEqual(runtime.state()["page_offset"], "20")
                self.assertEqual([c for e, c in runtime.http.requests if e == "indices"], [INDEX_SYMBOLS])
                self.assertEqual(runtime.http.request_headers[0], runtime.http.request_headers[1])
                self.assertFalse(any(t.startswith("CLOSED") for t in texts(tree)))

    def test_unknown_with_indices_uses_paid_budget_and_toggle_off_keeps_offset(self):
        runtime = Runtime()
        runtime.http.emit_limit = False
        first = runtime.render(show_indices=True)
        self.assertEqual(shown(first), SYMBOLS[:20])
        self.assertFalse(any("PRO" in t for t in texts(first)))
        runtime.clock.elapsed += 600
        before = len(runtime.http.requests)
        second = runtime.render(show_indices=False)
        self.assertEqual(shown(second), SYMBOLS[20:45])
        self.assertEqual([e for e, _ in runtime.http.requests[before:]], ["quotes"] * 5)
        self.assertEqual(shown(pages(second)[0]), SYMBOLS[20:25])

    def test_basic_indices_uses_cache_or_no_data_without_extra_request(self):
        for cached in (False, True):
            with self.subTest(cached=cached):
                runtime = Runtime()
                runtime.http.limit = 5
                if cached:
                    runtime.cache.values["last-indices:" + hashlib.sha256(b"synthetic-test").hexdigest()] = json.dumps({
                        "DJI": index_quote("DJI"), "SPX": dict(index_quote("SPX"), is_stale=True), "_old": ["DJI"]})
                tree = runtime.render(show_indices=True, plan_limit="5")
                self.assertEqual(runtime.http.requests, [("quotes", SYMBOLS[:5])])
                self.assertEqual(texts(pages(tree)[0])[0], "DOW")
                self.assertEqual(shown(tree), SYMBOLS[:5])
                self.assertFalse(any("PRO" in t for t in texts(tree)))
                if cached:
                    dimmed = [n for n in walk(pages(tree)[0]) if n["props"].get("color") == "#5a5a5a"]
                    self.assertEqual(len(dimmed), 4)
                else:
                    self.assertEqual(texts(pages(tree)[0]).count("no data"), 5)

    def test_indices_failure_preserves_quote_pages_and_both_caches(self):
        runtime = Runtime()
        runtime.render(show_indices=True)
        indices_key = "last-indices:" + hashlib.sha256(b"synthetic-test").hexdigest()
        self.assertIn("DJI", json.loads(runtime.cache.values[indices_key]))
        runtime.clock.elapsed += 600
        runtime.http.answers.clear()
        runtime.http.index_status = 502
        tree = runtime.render(show_indices=True)
        self.assertEqual(shown(tree), SYMBOLS[20:40])
        self.assertTrue(set(SYMBOLS[:40]) <= set(runtime.cache.last()))
        self.assertEqual(json.loads(runtime.cache.values[indices_key])["_old"], INDEX_SYMBOLS)
        self.assertTrue(any(n["props"].get("color") == "#5a5a5a" for n in walk(pages(tree)[0])))

    def test_indices_malformed_payload_does_not_break_quotes(self):
        for payload in ([], {"data": []}, {"data": {"DJI": None, "SPX": {"price": "bad"}, "NDX": 42}}):
            runtime = Runtime()
            runtime.http.index_body = payload
            tree = runtime.render(show_indices=True)
            self.assertEqual(shown(tree), SYMBOLS[:20])
            self.assertEqual(texts(pages(tree)[0]).count("no data"), 5)

    def test_missing_rut_vix_rows_preserve_other_indices_and_quotes(self):
        invalid = {"error": "Synthetic unavailable index", "price": None,
                   "change": "bad", "change_percentage": None}
        for missing in (("RUT",), ("VIX",), ("RUT", "VIX")):
            for entry in ("omitted", None, 42, {}, invalid):
                with self.subTest(missing=missing, entry=entry):
                    runtime = Runtime()
                    data = {s: index_quote(s) for s in INDEX_SYMBOLS}
                    for symbol in missing:
                        if entry == "omitted":
                            del data[symbol]
                        else:
                            data[symbol] = entry
                    runtime.http.index_body = {"data": data}
                    tree = runtime.render(show_indices=True)
                    rows = pages(tree)[0]["children"][0]["children"]
                    self.assertEqual([texts(row)[0] for row in rows], ["DOW", "S&P", "NDX", "RUT", "VIX"])
                    for symbol, row in zip(INDEX_SYMBOLS, rows):
                        self.assertEqual(texts(row)[1:], ["no data"] if symbol in missing else ["-10", "-0.5%"])
                    self.assertEqual(shown(tree), SYMBOLS[:20])
                    self.assertEqual([c for endpoint, c in runtime.http.requests if endpoint == "indices"], [INDEX_SYMBOLS])

    def test_indices_cache_coverage_requires_dict_for_every_symbol(self):
        for symbol in INDEX_SYMBOLS:
            for entry in ("omitted", None, 42, {}, index_quote(symbol)):
                with self.subTest(symbol=symbol, entry=entry):
                    runtime = Runtime()
                    runtime.render(symbols=",".join(SYMBOLS[:5]))
                    data = {s: index_quote(s) for s in INDEX_SYMBOLS}
                    if entry == "omitted":
                        del data[symbol]
                    else:
                        data[symbol] = entry
                    key = "last-indices:" + hashlib.sha256(b"synthetic-test").hexdigest()
                    runtime.cache.values[key] = json.dumps(data)
                    runtime.clock.elapsed += 600
                    before = len(runtime.http.requests)
                    tree = runtime.render(symbols=",".join(SYMBOLS[:5]), show_indices=True,
                                          fake_now_et="2026-11-02T21:00")
                    expected = [] if isinstance(entry, dict) else [("indices", INDEX_SYMBOLS)]
                    self.assertEqual(runtime.http.requests[before:], expected)
                    self.assertEqual(texts(pages(tree)[0]).count("no data"), int(entry == {}))

    def test_indices_are_saved_before_the_next_http_request(self):
        runtime = Runtime()
        def check_saved(symbols):
            if any(e == "indices" for e, _ in runtime.http.requests):
                key = "last-indices:" + hashlib.sha256(b"synthetic-test").hexdigest()
                self.assertIn("DJI", json.loads(runtime.cache.values[key]))
        runtime.http.before_request = check_saved
        runtime.render(show_indices=True)

    def test_missing_indices_wakes_covered_quotes_off_hours(self):
        runtime = Runtime()
        runtime.http.emit_limit = False
        runtime.render(symbols=",".join(SYMBOLS[:5]))
        runtime.clock.elapsed += 600
        tree = runtime.render(symbols=",".join(SYMBOLS[:5]), show_indices=True,
                              fake_now_et="2026-11-02T21:00")
        self.assertEqual(runtime.http.requests[-1], ("indices", INDEX_SYMBOLS))
        self.assertNotIn("no data", texts(pages(tree)[0]))

    def test_loopback_url_requires_whole_match(self):
        runtime = Runtime()
        for base in ("http://127.0.0.1:80@us-stock-etf-prices.p.rapidapi.com", "http://127.0.0.1:80/mock?x=1",
                     "http://127.0.0.1:80/mock\n", "http://127.0.0.1:80/mock\r",
                     "http://127.0.0.1:80/mock#fragment", "http://127.0.0.1:80/mock/../other"):
            self.assertEqual(runtime.env["api_url"](Config(api_base=base)),
                             "https://us-stock-etf-prices.p.rapidapi.com/v1/quotes")
        for base in ("http://127.0.0.1:80", "http://127.0.0.1:80/mock", "http://127.0.0.1:80/mock/nested"):
            self.assertEqual(runtime.env["api_url"](Config(api_base=base), "indices"), base + "/v1/indices")

    def test_default_gateway_requests_and_headers_without_overrides(self):
        runtime = Runtime()
        tree = runtime.env["main"](Config(rapidapi_key=" synthetic-test \n", symbols=",".join(SYMBOLS),
                                          fake_now_et="2026-11-02T10:15", show_indices=True))
        self.assertEqual(runtime.http.urls, [
            "https://us-stock-etf-prices.p.rapidapi.com/v1/quotes",
            "https://us-stock-etf-prices.p.rapidapi.com/v1/indices",
            "https://us-stock-etf-prices.p.rapidapi.com/v1/quotes",
            "https://us-stock-etf-prices.p.rapidapi.com/v1/quotes",
            "https://us-stock-etf-prices.p.rapidapi.com/v1/quotes",
        ])
        self.assertTrue(all(h == {"X-RapidAPI-Key": "synthetic-test",
                                  "X-RapidAPI-Host": "us-stock-etf-prices.p.rapidapi.com"}
                            for h in runtime.http.request_headers))
        self.assertEqual(shown(tree), SYMBOLS[:20])

    def test_api_url_overrides_support_full_quotes_url_and_base(self):
        runtime = Runtime()
        for override in ("http://127.0.0.1:12345/mock/v1/quotes", "http://127.0.0.1:12345/mock",
                         "http://127.0.0.1:12345/mock/"):
            for source in ("config", "constant"):
                with self.subTest(override=override, source=source):
                    runtime.env["API_URL"] = override if source == "constant" else ""
                    config = Config(api_url=override) if source == "config" else Config()
                    self.assertEqual(runtime.env["api_url"](config), "http://127.0.0.1:12345/mock/v1/quotes")
                    self.assertEqual(runtime.env["api_url"](config, "indices"), "http://127.0.0.1:12345/mock/v1/indices")
        runtime.env["API_URL"] = "http://127.0.0.1:12345/constant"
        self.assertEqual(runtime.env["api_url"](Config(api_url="http://127.0.0.1:12345/config")),
                         "http://127.0.0.1:12345/config/v1/quotes")
        self.assertEqual(runtime.env["api_url"](Config(api_base="http://127.0.0.1:12345/mock")),
                         "http://127.0.0.1:12345/mock/v1/quotes")

    def test_schema_keeps_secret_key_toggles_and_symbol_validation(self):
        runtime = Runtime()
        fields = {field["id"]: field for field in runtime.env["get_schema"]()["fields"]}
        self.assertEqual(set(fields), {"rapidapi_key", "symbols", "show_indices", "extended"})
        self.assertEqual(fields["rapidapi_key"]["kind"], "Text")
        self.assertIs(fields["rapidapi_key"]["secret"], True)
        self.assertEqual(fields["symbols"]["default"], "SPY,QQQ,AAPL,MSFT,NVDA")
        self.assertEqual(fields["show_indices"]["kind"], "Toggle")
        self.assertIs(fields["show_indices"]["default"], True)
        self.assertEqual(fields["extended"]["kind"], "Toggle")
        self.assertIs(fields["extended"]["default"], False)
        tokens, more = runtime.env["parse_symbols"](",".join(SYMBOLS + ["EXTRA"]))
        self.assertEqual(tokens, [(s, True) for s in SYMBOLS])
        self.assertEqual(more, 1)
        self.assertEqual(runtime.env["parse_symbols"]("$aapl,BRK-B,AAPL,123"),
                         ([("AAPL", True), ("BRK.B", True), ("123", False)], 0))

    def test_basic_http_cache_lasts_two_hours_paid_stays_nine_minutes(self):
        for limit, ttl in ((5, 7200), (100000, 540), (0, 540)):
            with self.subTest(limit=limit):
                runtime = Runtime()
                runtime.http.limit = limit
                runtime.render(plan_limit=str(limit), symbols=",".join(SYMBOLS[:5]))
                self.assertEqual(runtime.http.ttls, [ttl])

    def test_closed_market_without_usable_data_returns_empty_and_prints_state(self):
        for now, extended in (("2026-11-07T10:15", False), ("2026-11-02T03:00", True),
                              ("2026-11-02T21:00", False), ("2026-11-02T18:00", False)):
            for indices in (False, True):
                for payload in ({}, {s: dict(quote(s), price=None) for s in SYMBOLS[:5]}):
                    with self.subTest(now=now, extended=extended, indices=indices, payload=bool(payload)):
                        runtime = Runtime()
                        runtime.http.status_override = 502
                        runtime.http.body_overrides = {"data": payload}
                        root = runtime.render(symbols=",".join(SYMBOLS[:5]), fake_now_et=now,
                                              extended=extended, show_indices=indices)
                        self.assertEqual(root, [])
                        self.assertEqual(sum(s.startswith("STATE ") for s in runtime.lines), 1)
                        self.assertEqual(runtime.state()["page_offset"], "0")

    def test_closed_market_setup_stays_visible_without_http_or_writes(self):
        runtime = Runtime()
        root = runtime.render(rapidapi_key="", fake_now_et="2026-11-07T10:15")
        self.assertIn("ADD RAPIDAPI KEY", texts(root))
        self.assertEqual(runtime.http.requests, [])
        self.assertEqual(runtime.cache.writes, [])
        runtime.state()

    def test_closed_market_keeps_cached_quotes_or_indices_visible(self):
        for indices in (False, True):
            runtime = Runtime()
            runtime.render(symbols=",".join(SYMBOLS[:5]), show_indices=indices)
            if indices:
                for key in list(runtime.cache.values):
                    if key.startswith("last:"):
                        del runtime.cache.values[key]
            runtime.http.answers.clear()
            runtime.http.status_override = 502
            root = runtime.render(symbols=",".join(SYMBOLS[:5]), show_indices=indices,
                                  fake_now_et="2026-11-07T10:15")
            self.assertIn("DOW" if indices else SYMBOLS[0], texts(root))
            # Fresh quote reuse keeps its colors; cached indices remain dim.
            self.assertEqual(any(n["props"].get("color") == "#5a5a5a" for n in walk(root)), indices)

    def test_in_hours_and_enabled_extended_failure_fallback_remains_visible(self):
        for now, extended in (("2026-11-02T10:15", False), ("2026-11-02T18:00", True),
                              ("2026-11-02T09:30", False), ("2026-11-02T09:49", False),
                              ("2026-11-02T04:00", True)):
            for indices in (False, True):
                runtime = Runtime()
                runtime.http.status_override = 502
                root = runtime.render(fake_now_et=now, extended=extended, show_indices=indices)
                self.assertNotEqual(root, [])
                self.assertIn("no data" if indices else "SERVICE DOWN", texts(root))
                runtime.state()

    def test_closed_market_quota_and_burst_notices_remain_visible(self):
        for quota in (False, True):
            for indices in (False, True):
                runtime = Runtime()
                runtime.http.quota = quota
                runtime.http.burst = not quota
                root = runtime.render(fake_now_et="2026-11-07T10:15", show_indices=indices)
                self.assertIn("QUOTA USED UP" if quota else "RATE LIMITED", texts(root))
                if quota:
                    self.assertIn(runtime.env["UPGRADE_URL"], texts(root))
                    # Persisted quota notices must also survive a closed-market render.
                    root = runtime.render(fake_now_et="2026-11-07T10:15", show_indices=indices)
                    self.assertIn(runtime.env["UPGRADE_URL"], texts(root))

    def test_known_holiday_without_data_is_empty_but_cached_data_still_shows(self):
        runtime = Runtime()
        key = hashlib.sha256(b"synthetic-test").hexdigest()
        runtime.cache.values["dormant:" + key] = "1"
        runtime.http.status_override = 502
        self.assertEqual(runtime.render(), [])
        tokens = [(s, True) for s in SYMBOLS]
        lh = hashlib.sha256(("synthetic-test" + ",".join(s for s, _ in tokens)).encode()).hexdigest()
        runtime.cache.values["last:" + lh] = json.dumps({SYMBOLS[0]: quote(SYMBOLS[0])})
        self.assertIn(SYMBOLS[0], texts(runtime.render()))

    def test_known_holiday_with_invalid_symbols_and_no_data_is_empty(self):
        runtime = Runtime()
        runtime.cache.values["dormant:" + hashlib.sha256(b"synthetic-test").hexdigest()] = "1"
        self.assertEqual(runtime.render(symbols="123,!"), [])
        self.assertEqual(runtime.http.requests, [])

    def test_closed_market_without_fetch_keeps_persisted_quota_notice(self):
        for indices, limit in ((False, 100000), (True, 5)):
            runtime = Runtime()
            key = hashlib.sha256(b"synthetic-test").hexdigest()
            now = runtime.clock.parse_time("2026-11-07T10:15")
            runtime.cache.values.update({"pause:" + key: str(now.unix + 3600), "lim:" + key: str(limit)})
            root = runtime.render(symbols="123,!", show_indices=indices, fake_now_et="2026-11-07T10:15")
            self.assertNotEqual(root, [])
            self.assertIn(runtime.env["UPGRADE_URL"], texts(root))
            self.assertEqual(runtime.http.requests, [])
            self.assertEqual(sum(s.startswith("STATE ") for s in runtime.lines), 1)

    def test_publish_app_is_byte_identical_to_root(self):
        self.assertEqual((ROOT / "publish/tronbyt/apps/stockwatchlist/stock_watchlist.star").read_bytes(),
                         (ROOT / "stock_watchlist.star").read_bytes())

    def test_preview_generator_rejects_live_executable_after_symlink_or_path_resolution(self):
        with tempfile.TemporaryDirectory(prefix=".run-preview-guard-", dir=ROOT / "tests") as scratch:
            work = Path(scratch)
            protected = work / ".stockwatchlist/bin/pixlet"
            protected.parent.mkdir(parents=True)
            marker = work / "executed"
            protected.write_text("#!/usr/bin/env python3\nfrom pathlib import Path\n"
                                 + "Path(%r).write_text('executed')\n" % str(marker)
                                 + "raise SystemExit(97)\n")
            protected.chmod(0o700)
            alias = work / "shortcut"
            alias.symlink_to(protected)
            for executable in (str(alias), "pixlet"):
                env = dict(os.environ, PIXLET=executable, HOME=str(work), TMPDIR=str(work),
                           PATH=str(protected.parent) + os.pathsep + os.environ.get("PATH", ""))
                result = subprocess.run([os.sys.executable, str(ROOT / "tests/render_previews.py")],
                                        cwd=work, env=env, text=True, capture_output=True, timeout=10)
                self.assertFalse(marker.exists(), "protected executable was invoked")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("outside the live installation", result.stderr)

    def test_public_app_urls_have_no_other_service_hosts(self):
        runtime = Runtime()
        self.assertEqual(runtime.env["api_url"](Config()),
                         "https://us-stock-etf-prices.p.rapidapi.com/v1/quotes")
        self.assertEqual(runtime.env["api_url"](Config(), "indices"),
                         "https://us-stock-etf-prices.p.rapidapi.com/v1/indices")
        paths = [ROOT / "stock_watchlist.star", ROOT / "README.md"]
        paths += [p for folder in (ROOT / "tests", ROOT / "publish") for p in folder.rglob("*")
                  if p.is_file() and p.suffix in (".star", ".py", ".md", ".yaml")
                  and not any(part.startswith(".run-") for part in p.parts)]
        allowed = {"127.0.0.1", "us-stock-etf-prices.p.rapidapi.com", "rapidapi.com",
                   "github.com", "raw.githubusercontent.com", "tronbyt.com"}
        for path in paths:
            source = re.sub(r"^LOOPBACK_RE = .*", "", path.read_text(), flags=re.M)
            for url in re.findall(r'https?://[^\s\x22\x27<>`]+', source):
                self.assertTrue(urlsplit(url).hostname in allowed,
                                "unexpected URL host in " + str(path.relative_to(ROOT)))
            for address in re.findall(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", source):
                self.assertTrue(ipaddress.ip_address(address).is_loopback,
                                "non-loopback address in " + str(path.relative_to(ROOT)))

    def test_pixlet_regex_double_returns_capture_groups_only(self):
        runtime = Runtime()
        match = runtime.env["re"].match
        self.assertEqual(match(runtime.env["LOOPBACK_RE"], "http://127.0.0.1:80/mock"), ["/mock"])
        self.assertEqual(match(runtime.env["SYMBOL_RE"], "BRK.B"), [".B"])
        self.assertEqual(match(runtime.env["SYMBOL_RE"], "AAPL"), [""])
        self.assertEqual(match("^AAPL$", "AAPL"), [])
        self.assertEqual(match(runtime.env["SYMBOL_RE"], "123"), [])

    def test_cached_indices_are_dim_when_basic_cannot_refresh_them(self):
        runtime = Runtime()
        runtime.http.limit = 5
        key = "last-indices:" + hashlib.sha256(b"synthetic-test").hexdigest()
        runtime.cache.values[key] = json.dumps({s: index_quote(s) for s in INDEX_SYMBOLS})
        tree = runtime.render(show_indices=True, plan_limit="5")
        self.assertEqual(sum(n["props"].get("color") == "#5a5a5a" for n in walk(pages(tree)[0])), 10)
        self.assertEqual(runtime.http.requests, [("quotes", SYMBOLS[:5])])

    def test_all_invalid_tickers_do_not_prevent_indices_fetch(self):
        runtime = Runtime()
        tree = runtime.render(symbols="123,!", show_indices=True)
        self.assertEqual(runtime.http.requests, [("indices", INDEX_SYMBOLS)])
        self.assertNotIn("no data", texts(pages(tree)[0]))
        self.assertEqual(texts(tree).count("invalid"), 2)

    def test_success_with_zero_remaining_never_pauses_or_adds_quota_notices(self):
        for indices in (False, True):
            for limit in (None, 5, 100000):
                for remaining, metadata in ((0, {}), (0, {"message": "daily quota exhausted"}),
                                            (4, {"error": "daily quota exceeded"})):
                    with self.subTest(indices=indices, limit=limit, remaining=remaining, metadata=metadata):
                        runtime = Runtime()
                        runtime.http.emit_limit = limit is not None
                        runtime.http.limit = limit or 100000
                        runtime.http.remaining = remaining
                        runtime.http.body_overrides = metadata
                        tree = runtime.render(show_indices=indices)
                        self.assertEqual(runtime.state()["plan_limit"], str(limit or 0))
                        self.assertFalse(any(k.startswith("pause:") for k in runtime.cache.values))
                        self.assertFalse(any("PRO" in t or "QUOTA" in t or "USED" in t or "RATE LIMITED" in t
                                             for t in texts(tree)))
                        self.assertNotIn(runtime.env["UPGRADE_URL"], texts(tree))
                        count = 5 if limit == 5 else 20 if indices else 25
                        self.assertEqual(shown(tree), SYMBOLS[:count])
                        self.assertEqual(len(runtime.http.requests), 1 if limit == 5 else 5)
                        if indices and limit != 5:
                            self.assertNotIn("no data", texts(pages(tree)[0]))

    def test_failed_origin_quota_responses_pause_and_notice(self):
        for status in (200, 429, 403):
            with self.subTest(status=status):
                runtime = Runtime()
                runtime.http.emit_headers = False
                runtime.http.status_override = status
                runtime.http.body_overrides = {"data": {}, "error": "daily quota exceeded"}
                tree = runtime.render()
                self.assertTrue(any(k.startswith("pause:") for k in runtime.cache.values))
                self.assertTrue(any("USED" in t for t in texts(tree)))
                self.assertIn(runtime.env["UPGRADE_URL"], texts(tree))
                self.assertEqual(runtime.state()["page_offset"], "0")
                self.assertEqual(runtime.state()["plan_limit"], "5")
                runtime.render()
                self.assertEqual(len(runtime.http.requests), 1)

    def test_per_second_quota_429_keeps_unknown_and_paid_plans_and_releases_claim(self):
        for plan in (0, 100000):
            with self.subTest(plan=plan):
                runtime = Runtime()
                runtime.http.emit_limit = False
                runtime.http.burst = True
                runtime.http.remaining = 90000
                runtime.http.error_body = {"message": "Per-second quota exceeded; retry in one second"}
                runtime.http.header_overrides = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1"}
                for _ in range(2):
                    tree = runtime.render(plan_limit=str(plan), fake_now_et="2026-11-02T21:00")
                    self.assertEqual(runtime.state()["plan_limit"], str(plan))
                    self.assertFalse(any(k.startswith("pause:") for k in runtime.cache.values))
                    self.assertIn("RATE LIMITED", texts(tree))
                    self.assertFalse(any("PRO" in t or "QUOTA" in t or "FREE" in t for t in texts(tree)))
                    self.assertNotIn(runtime.env["UPGRADE_URL"], texts(tree))
                self.assertEqual(len(runtime.http.requests), 2)  # the unbilled burst releases the cold claim

    def test_429_exhausted_plan_budget_pauses_without_quota_wording(self):
        for plan in (0, 100000):
            with self.subTest(plan=plan):
                runtime = Runtime()
                runtime.http.emit_limit = False
                runtime.http.burst = True
                runtime.http.remaining = 0
                runtime.http.error_body = {"message": "Too many requests"}
                tree = runtime.render(plan_limit=str(plan))
                self.assertEqual(runtime.state()["plan_limit"], "5" if plan == 0 else "100000")
                key = "pause:" + hashlib.sha256(b"synthetic-test").hexdigest()
                self.assertEqual(int(runtime.cache.values[key]), runtime.clock.now().unix + 3600)
                self.assertTrue(any("USED" in t for t in texts(tree)))
                self.assertNotIn("RATE LIMITED", texts(tree))
                runtime.render()
                self.assertEqual(len(runtime.http.requests), 1)

    def test_burst_evidence_and_positive_request_budget_override_quota_wording(self):
        short = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1"}
        for remaining, headers, message in (
            (90000, {}, "daily quota exceeded"),
            (None, {}, "Per second quota exceeded"),
            (None, short, "quota exhausted"),
            (None, {}, "See quota details"),
        ):
            with self.subTest(remaining=remaining, headers=headers, message=message):
                runtime = Runtime()
                runtime.http.emit_headers = False
                runtime.http.burst = True
                runtime.http.header_overrides = dict(headers)
                if remaining is not None:
                    runtime.http.header_overrides["X-Ratelimit-Requests-Remaining"] = str(remaining)
                runtime.http.error_body = {"message": message}
                tree = runtime.render()
                self.assertEqual(runtime.state()["plan_limit"], "0")
                self.assertFalse(any(k.startswith("pause:") for k in runtime.cache.values))
                self.assertIn("RATE LIMITED", texts(tree))
                self.assertFalse(any("PRO" in t or "QUOTA" in t or "FREE" in t for t in texts(tree)))
                self.assertNotIn(runtime.env["UPGRADE_URL"], texts(tree))

    def test_zero_remaining_on_non_quota_http_error_does_not_pause(self):
        runtime = Runtime()
        runtime.http.status_override = 502
        runtime.http.remaining = 0
        runtime.http.body_overrides = {"data": {}, "error": "quota metrics unavailable"}
        tree = runtime.render(symbols=",".join(SYMBOLS[:5]))
        self.assertFalse(any(k.startswith("pause:") for k in runtime.cache.values))
        self.assertFalse(any("PRO" in t or "QUOTA" in t or "FREE" in t for t in texts(tree)))

    def test_basic_daily_success_guard_does_not_show_upgrade_notice(self):
        runtime = Runtime()
        runtime.http.limit = 5
        runtime.render(plan_limit="5")
        key = hashlib.sha256(b"synthetic-test").hexdigest()
        runtime.cache.values["calls:" + key] = ",".join([str(runtime.clock.now().unix)] * 5)
        tree = runtime.render(fake_now_et="2026-11-02T11:50")
        self.assertFalse(any("PRO" in t or "USED" in t for t in texts(tree)))
        self.assertEqual(len(runtime.http.requests), 1)

    def test_indices_share_elapsed_budget_and_short_dwell(self):
        runtime = Runtime()
        runtime.http.delay = 4.9
        tree = runtime.render(show_indices=True)
        self.assertLessEqual(runtime.clock.elapsed, 15)
        self.assertEqual(len(runtime.http.requests), 3)
        self.assertEqual(shown(tree), SYMBOLS[:10])
        self.assertEqual(runtime.state()["page_offset"], "10")
        for dwell in (1500, 3000, 4500):
            runtime = Runtime(dwell)
            tree = runtime.render(show_indices=True)
            self.assertEqual(texts(pages(tree)[0])[0], "DOW")
            self.assertTrue(shown(pages(tree)[1]))
            self.assertLessEqual(len(pages(tree)) * tree["props"]["delay"], dwell)

    def test_basic_learned_from_indices_counts_both_requests_in_daily_guard(self):
        runtime = Runtime()
        runtime.http.emit_limit = False
        def learn_basic(symbols):
            if symbols == INDEX_SYMBOLS:
                runtime.http.emit_limit, runtime.http.limit = True, 5
        runtime.http.before_request = learn_basic
        tree = runtime.render(show_indices=True)
        self.assertEqual(len(runtime.http.requests), 2)
        self.assertEqual(runtime.state()["plan_limit"], "5")
        self.assertEqual(shown(tree), SYMBOLS[:5])
        key = "calls:" + hashlib.sha256(b"synthetic-test").hexdigest()
        self.assertEqual(len(runtime.cache.values[key].split(",")), 2)
        runtime.http.before_request = None
        for slot in ("11:50", "13:50", "15:50", "16:25"):
            runtime.clock.elapsed += 600
            runtime.http.answers.clear()
            runtime.render(show_indices=True, fake_now_et="2026-11-02T" + slot)
        self.assertEqual(len(runtime.http.requests), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
