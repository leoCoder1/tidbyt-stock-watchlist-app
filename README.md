# Stock Watchlist

A stock and ETF watchlist for Tidbyt and Tronbyt displays. Follow up to **100 US tickers**, with delayed prices, signed percentage changes, and market indices on page one. The app uses a 64×32 layout and rotates longer watchlists across renders.

## Screenshots

These screenshots use synthetic/example data, not live quotes. Each 64×32 render is enlarged 10× to 640×320.

![Market indices page showing DOW, S&P, NDX, RUT, and VIX with signed changes](docs/images/market-indices.png)

Market indices: signed point changes in the middle and percentage changes on the right.

![Ticker page showing SPY, QQQ, AAPL, MSFT, and NVDA with example prices and percentage changes](docs/images/ticker-page.png)

Ticker page: example prices and signed percentage changes.

![First-run setup screen prompting ADD RAPIDAPI KEY and FREE KEY WORKS, with the signup link starting to scroll](docs/images/add-rapidapi-key.png)

First-run setup: add a free RapidAPI key to get started.

## Get a free RapidAPI key

Get a free key at [US Stock & ETF Prices on RapidAPI](https://rapidapi.com/logicione/api/us-stock-etf-prices), then copy your RapidAPI key. The free plan works.

## Install on Tronbyt

Install **Stock Watchlist** from your Tronbyt community app catalog when available, or sideload the app from `publish/tronbyt/apps/stockwatchlist/` in this repository ([leoCoder1/tidbyt-stock-watchlist-app](https://github.com/leoCoder1/tidbyt-stock-watchlist-app)) using your instance's app import workflow. A pull request to add the app to the public Tronbyt catalog may be open or merged separately.

1. Paste the key into the **RapidAPI key** field.
2. Enter comma-separated **Tickers**, such as `SPY,QQQ,AAPL,MSFT,NVDA`. The app validates and deduplicates symbols, accepts forms such as `BRK.B`, and caps the list at 100. Invalid symbols appear on screen and are never sent to the quote service.
3. Leave **Market indices** on, or turn it off for tickers only.
4. Optionally enable **Pre-market & after hours** for extended-session updates.

With no key, the app shows **ADD RAPIDAPI KEY / FREE KEY WORKS**, scrolls the [RapidAPI signup link](https://rapidapi.com/logicione/api/us-stock-etf-prices), and makes **zero API calls**. It writes no caches or paging state until a key is entered.

## Display and refresh behavior

**Market indices** defaults to on. Page one shows **DOW**, **S&P**, **NDX**, **RUT**, and **VIX**, in that order. Signed point changes occupy the middle column, with signed percent changes on the right. Ticker prices start on the next page, with prices in the middle and percentages on the right. Positive and negative changes use green and red; zero is neutral. Missing point changes fall back to price minus previous close, then `--` when unavailable. There is no CLOSED header or page-number footer.

Each render makes at most five requests, with up to five symbols per request. The indices batch shares that budget, leaving room for up to 20 tickers; ticker-only renders can fetch up to 25. Longer lists continue on later renders, and shorter display dwells fetch only what fits.

On Tronbyt, keep persistent HTTP caching enabled. Frequent re-renders are not always new API calls; the manifest's recommended interval is ten minutes.

Fresh quotes keep their normal colors when reused between scheduled fetches or served from the HTTP cache. Stale or old quotes, quotes during a quota pause, and cached indices remain visible and dimmed. With a key set, a fully closed market and no usable cached data or notices can skip rendering. In an active regular or enabled extended session, failed fetches retain a visible no-data or **SERVICE DOWN** fallback. Setup and quota/rate-limit notices stay visible. Market windows include a delayed-data tail; holidays use the existing response-based dormancy detection.

Quota and rate-limit notices appear only after a real error. Quota exhaustion pauses calls and scrolls the [RapidAPI link](https://rapidapi.com/logicione/api/us-stock-etf-prices). A short burst HTTP 429 shows **RATE LIMITED** and a retry message.

## Market indices

The first page has five rows:

| Display | API ticker | Full name and meaning |
| --- | --- | --- |
| DOW | DJI | Dow Jones Industrial Average — 30 large US blue chips |
| S&P | SPX | S&P 500 — 500 leading large US companies |
| NDX | NDX | Nasdaq-100 — 100 large nonfinancial companies listed on Nasdaq |
| RUT | RUT | Russell 2000 — US small caps |
| VIX | VIX | Cboe Volatility Index — expected 30-day S&P 500 volatility, often called the "fear gauge" |

The middle column shows the **signed point change**, and the right column shows the **signed percent change**. Small point moves retain one decimal, such as VIX `+0.4`; moves of 10 points or more use whole points. A missing or unusable index entry retains a dimmed last-good value when one exists; otherwise it shows `no data`. Other rows remain visible.

All five plain tickers use one `/v1/indices` request, matching the API's five-index cap. This single batch shares the existing five-request render budget.

## Run from this repository with Pixlet

Clone this repository from [https://github.com/leoCoder1/tidbyt-stock-watchlist-app](https://github.com/leoCoder1/tidbyt-stock-watchlist-app). Use a compatible [Tronbyt Pixlet](https://github.com/tronbyt/pixlet) executable. Set `PIXLET` to its path. Keep your configuration JSON out of git: `config/` and `local/` are ignored. Create `config/watchlist.json` locally with this shape, then enter your key in your editor:

```json
{
  "rapidapi_key": "",
  "symbols": "SPY,QQQ,AAPL,MSFT,NVDA",
  "show_indices": "true",
  "extended": "false"
}
```

```sh
export PIXLET=/path/to/pixlet
"$PIXLET" render stock_watchlist.star -c config/watchlist.json
"$PIXLET" serve stock_watchlist.star --saveconfig config/watchlist.json
```

`render` reads the local JSON and writes a WebP. `serve` opens the local configuration UI; enter your settings there, and `--saveconfig` saves changes to the ignored file. Keep that file confidential. These commands render or preview; use your display's sideload workflow to install the result.

The technical defaults are `https://us-stock-etf-prices.p.rapidapi.com/v1/quotes` and `https://us-stock-etf-prices.p.rapidapi.com/v1/indices`. Requests carry `X-RapidAPI-Key` and `X-RapidAPI-Host`.

For an existing local Tidbyt setup, `push_tidbyt.sh` renders the root `stock_watchlist.star` and pushes the image using your locally supplied settings. Configure `PIXLET`, `STOCKS_APP`, and `STOCKS_CONFIG` for your own paths and keep authentication/device settings outside git. An existing launchd job can invoke that script during market windows; this repository does not install or change a job. The script preserves the next ticker offset and detected limit via the app's `STATE page_offset=<n> plan_limit=<n>` output, updating its local state only after a successful push. `STATE_DIR` can select a separate state directory. Last-good data and scheduling guards across processes require persistent Pixlet caching, such as your own Redis configured through `PIXLET_REDIS_URL`.

## Privacy

The RapidAPI key is a **secret schema field** and, in the default app, is sent only to RapidAPI as `X-RapidAPI-Key`. Requests include the selected ticker symbols; no other personal data is sent. Your configured key and watchlist stay in your local or Tronbyt settings and should never be committed. Shared app-cache keys incorporate a hash of the API key to keep installations separate. Treat local configuration and HTTP caches as confidential.

## Tests and community package

Run `bash tests/run.sh` for offline logic and push-state tests. To include real Pixlet renders against synthetic loopback data, set `PIXLET` to an executable outside any live installation. See [tests/README.md](tests/README.md) for requirements and isolation details. The tests never need a real key or a device and never call the real quote service.

The ready-to-copy package is `publish/tronbyt/apps/stockwatchlist/`, containing the manifest, a byte-identical copy of the root app, and mock-data WebP/GIF previews. The suite checks the two app copies stay identical. After editing the root app, run `pixlet format` and recopy it into the package before testing. See Tronbyt's [publishing guide](https://tronbyt.com/pixlet/publish/publishing-apps/) for the community submission workflow.

## Feedback and bugs

- Users of this repository can open an Issue in the [Issues tab](https://github.com/leoCoder1/tidbyt-stock-watchlist-app/issues).
- Tronbyt store / catalog users can also report issues at [Tronbyt apps Issues](https://github.com/tronbyt/apps/issues).
- Questions about the API or data go to the [RapidAPI listing's Discussions tab](https://rapidapi.com/logicione/api/us-stock-etf-prices).
