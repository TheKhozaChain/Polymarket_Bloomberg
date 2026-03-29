#!/usr/bin/env python3
"""
POLYMARKET TERMINAL — Bloomberg-style real-time dashboard for Polymarket
════════════════════════════════════════════════════════════════════════════
Usage:  python3 terminal.py
Keys:   r=refresh  1/2/3=leaderboard(day/week/all)  q=quit
"""

import asyncio
import json
import random
from collections import deque
from datetime import datetime
from typing import Any

import httpx
from rich import box
from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.widgets import Footer, Static

# ── API endpoints ──────────────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

# ── Formatting helpers ─────────────────────────────────────────────────────


def fmt_vol(n: float) -> str:
    if n is None:
        return "\u2014"
    a = abs(n)
    s = "-" if n < 0 else ""
    if a >= 1_000_000_000:
        return f"{s}${a / 1e9:.1f}B"
    if a >= 1_000_000:
        return f"{s}${a / 1e6:.1f}M"
    if a >= 1_000:
        return f"{s}${a / 1e3:.1f}K"
    return f"{s}${a:,.0f}"


def fmt_cents(p: float) -> str:
    return f"{p * 100:.1f}\u00a2"


def fmt_pnl(n: float) -> str:
    prefix = "+" if n >= 0 else ""
    if abs(n) >= 1_000:
        return fmt_vol(n)
    return f"{prefix}${n:,.0f}"


def fmt_pct(n: float) -> str:
    prefix = "+" if n >= 0 else ""
    return f"{prefix}{n:.2f}%"


def trunc(s: str, w: int) -> str:
    return s if len(s) <= w else s[: w - 1] + "\u2026"


def bar_block(pct: float, width: int = 8) -> str:
    filled = max(0, min(width, int(pct * width)))
    return "\u2588" * filled + "\u2591" * (width - filled)


def fmt_size(n: float) -> str:
    """Format size compactly to avoid column overflow."""
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 10_000:
        return f"{n / 1e3:.0f}K"
    if n >= 1_000:
        return f"{n / 1e3:.1f}K"
    return f"{n:,.0f}"


# ── API Client ─────────────────────────────────────────────────────────────


class PolyAPI:
    def __init__(self):
        self.http = httpx.AsyncClient(timeout=15.0, follow_redirects=True)

    async def events(self, limit: int = 25) -> list[dict]:
        try:
            r = await self.http.get(
                f"{GAMMA_API}/events",
                params=dict(
                    limit=limit, order="volume24hr", ascending="false",
                    active="true", closed="false",
                ),
            )
            r.raise_for_status()
            return r.json()
        except Exception:
            return []

    async def markets(self, limit: int = 50) -> list[dict]:
        try:
            r = await self.http.get(
                f"{GAMMA_API}/markets",
                params=dict(
                    limit=limit, order="volume24hr", ascending="false",
                    active="true", closed="false",
                ),
            )
            r.raise_for_status()
            return r.json()
        except Exception:
            return []

    async def order_book(self, token_id: str) -> dict:
        try:
            r = await self.http.get(
                f"{CLOB_API}/book", params=dict(token_id=token_id)
            )
            r.raise_for_status()
            return r.json()
        except Exception:
            return {"bids": [], "asks": []}

    async def leaderboard(self, period: str = "day", limit: int = 10) -> list[dict]:
        try:
            r = await self.http.get(
                f"{DATA_API}/v1/leaderboard",
                params=dict(period=period, limit=limit, orderBy="pnl"),
            )
            r.raise_for_status()
            return r.json()
        except Exception:
            return []

    _crypto_cache: dict = {}

    async def crypto_prices(self) -> dict:
        for url, parser in [
            ("https://api.coingecko.com/api/v3/simple/price", self._parse_coingecko),
            ("https://api.coincap.io/v2/assets", self._parse_coincap),
        ]:
            try:
                params = (
                    dict(ids="bitcoin,ethereum,solana,ripple", vs_currencies="usd", include_24hr_change="true")
                    if "coingecko" in url
                    else dict(ids="bitcoin,ethereum,solana,xrp")
                )
                r = await self.http.get(url, params=params)
                if r.status_code == 200:
                    data = parser(r.json())
                    if data:
                        self._crypto_cache = data
                        return data
            except Exception:
                pass
        if self._crypto_cache:
            return self._crypto_cache
        return {
            "bitcoin": {"usd": 69495, "usd_24h_change": 0.02},
            "ethereum": {"usd": 2077.21, "usd_24h_change": -0.04},
            "solana": {"usd": 87.64, "usd_24h_change": 0.05},
            "xrp": {"usd": 1.37, "usd_24h_change": -0.01},
        }

    @staticmethod
    def _parse_coingecko(data: dict) -> dict:
        if "ripple" in data:
            data["xrp"] = data.pop("ripple")
        return data

    @staticmethod
    def _parse_coincap(data: dict) -> dict:
        result = {}
        for a in data.get("data", []):
            result[a["id"]] = {
                "usd": float(a.get("priceUsd", 0)),
                "usd_24h_change": float(a.get("changePercent24Hr", 0) or 0),
            }
        return result


# ── Parse events into market rows ──────────────────────────────────────────


def parse_markets(events: list[dict]) -> list[dict]:
    rows = []
    for ev in events:
        title = ev.get("title", "")
        markets = ev.get("markets", []) or []
        total_vol = 0.0
        best = None
        best_score = -1

        for m in markets:
            try:
                total_vol += float(m.get("volume", 0) or 0)
            except Exception:
                pass
            try:
                prices = m.get("outcomePrices", "[]")
                if isinstance(prices, str):
                    prices = json.loads(prices)
                if not prices or len(prices) < 2:
                    continue
                yp, np_ = float(prices[0]), float(prices[1])
                tokens = m.get("clobTokenIds", "[]")
                if isinstance(tokens, str):
                    tokens = json.loads(tokens)
                token_id = str(tokens[0]) if tokens else ""
                vol24 = float(m.get("volume24hr", 0) or 0)
                balance = 1.0 - abs(yp - 0.5) * 2
                score = vol24 * (0.3 + 0.7 * balance)
                if score > best_score:
                    best_score = score
                    best = dict(yes=yp, no=np_, token_id=token_id)
            except Exception:
                pass

        if best and 0.01 < best["yes"] < 0.99:
            rows.append(dict(title=title, yes=best["yes"], no=best["no"], vol=total_vol, token_id=best["token_id"]))
        elif markets:
            m0 = markets[0]
            try:
                prices = m0.get("outcomePrices", "[]")
                if isinstance(prices, str):
                    prices = json.loads(prices)
                yp = float(prices[0]) if prices else 0.0
                np_ = float(prices[1]) if len(prices) >= 2 else 1.0 - yp
                tokens = m0.get("clobTokenIds", "[]")
                if isinstance(tokens, str):
                    tokens = json.loads(tokens)
                tid = str(tokens[0]) if tokens else ""
            except Exception:
                yp, np_, tid = 0.0, 0.0, ""
            rows.append(dict(title=title, yes=yp, no=np_, vol=total_vol, token_id=tid))
        else:
            rows.append(dict(title=title, yes=0.0, no=0.0, vol=total_vol, token_id=""))
    rows.sort(key=lambda x: x["vol"], reverse=True)
    return rows


def find_orderbook_candidates(raw_markets: list[dict]) -> list[dict]:
    candidates = []
    for m in raw_markets:
        try:
            prices = m.get("outcomePrices", "[]")
            if isinstance(prices, str):
                prices = json.loads(prices)
            tokens = m.get("clobTokenIds", "[]")
            if isinstance(tokens, str):
                tokens = json.loads(tokens)
            if not prices or len(prices) < 2 or not tokens:
                continue
            yp = float(prices[0])
            if yp < 0.08 or yp > 0.92:
                continue
            vol24 = float(m.get("volume24hr", 0) or 0)
            candidates.append(dict(
                question=m.get("question", ""), token_id=str(tokens[0]),
                yes=yp, vol24=vol24,
            ))
        except Exception:
            pass
    candidates.sort(key=lambda x: x["vol24"], reverse=True)
    return candidates


# ── Panel builders ─────────────────────────────────────────────────────────


def build_markets_table(markets: list[dict]) -> Group:
    hdr = Text()
    hdr.append(" \u25c6 ", style="bold red")
    hdr.append("MARKETS", style="bold red")

    t = Table(
        box=box.SIMPLE_HEAD, expand=True, show_edge=False,
        pad_edge=False, padding=(0, 1), header_style="bold white",
    )
    t.add_column("MARKET", ratio=3, no_wrap=True)
    t.add_column("YES", justify="right", width=7)
    t.add_column("NO", justify="right", width=7)
    t.add_column("CHART", width=10)
    t.add_column("VOL", justify="right", width=9)

    for m in markets[:22]:
        y, n = m["yes"], m["no"]
        t.add_row(
            trunc(m["title"], 36),
            Text(fmt_cents(y), style="bold green" if y > 0.5 else "green"),
            Text(fmt_cents(n), style="bold red" if n > 0.5 else "red"),
            Text(bar_block(y, 8), style="green"),
            Text(fmt_vol(m["vol"]), style="cyan"),
        )
    return Group(hdr, t)


def normalize_book(book: dict) -> tuple[list[dict], list[dict]]:
    bids = sorted(book.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
    asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]))
    return bids, asks


def build_orderbooks(book_data: list[tuple[str, dict, float]]) -> Group:
    now = datetime.now().strftime("%H:%M:%S")
    hdr = Text()
    hdr.append(" \u25c6 ", style="bold green")
    hdr.append("LIVE ORDERBOOKS", style="bold green")
    hdr.append(f"  {now}", style="dim white")

    parts: list[Any] = [hdr, Text("")]

    for title, book, ref_price in book_data[:3]:
        bids, asks = normalize_book(book)

        bid_total = sum(float(b["size"]) for b in bids)
        ask_total = sum(float(a["size"]) for a in asks)
        total = bid_total + ask_total or 1

        best_bid = float(bids[0]["price"]) if bids else 0
        best_ask = float(asks[0]["price"]) if asks else 1

        if best_bid > 0 and best_ask < 1:
            mid = (best_bid + best_ask) / 2
            spread = best_ask - best_bid
        else:
            mid = ref_price
            spread = best_ask - best_bid if best_bid > 0 else 0

        spread_bps = int(spread * 10000) if spread > 0 else 0
        bid_pct = bid_total / total * 100 if total else 50

        bid_dollars = sum(float(b["size"]) * float(b["price"]) for b in bids)
        ask_dollars = sum(float(a["size"]) * float(a["price"]) for a in asks)

        # ── Market header ──
        info = Text()
        info.append(trunc(title, 50), style="bold white")
        info.append("\n")
        info.append(f"MID:{fmt_cents(mid)}", style="yellow")
        info.append(f"  SPRD:{spread * 100:.1f}\u00a2 ({spread_bps}bps)", style="white")
        info.append(f"  IMBAL:{int(bid_pct)}%", style="cyan")
        info.append("\n")

        # ── Imbalance bar ──
        bw = max(1, int(bid_pct * 36 / 100))
        aw = max(1, 36 - bw)
        info.append(f"BIDS:${bid_dollars:,.0f} ({len(bids)}lvl) ", style="green")
        info.append("\u2588" * bw, style="green")
        info.append("\u2588" * aw, style="red")
        info.append(f" ASKS:${ask_dollars:,.0f} ({len(asks)}lvl)", style="red")
        info.append("\n")
        info.append(f"{'':>{bw + 18}}{int(bid_pct)}%", style="dim")

        # ── Depth table ──
        dt = Table(
            box=None, expand=True, show_header=True, show_edge=False,
            pad_edge=False, padding=(0, 1), header_style="dim white",
        )
        dt.add_column("CUM$", justify="right", width=8)
        dt.add_column("SIZE", justify="right", width=7)
        dt.add_column("", width=4)        # bid bar
        dt.add_column("BID", justify="right", width=6, style="green")
        dt.add_column("ASK", justify="left", width=6, style="red")
        dt.add_column("", width=4)        # ask bar
        dt.add_column("SIZE", justify="right", width=7)
        dt.add_column("CUM$", justify="right", width=8)

        n_rows = 8
        show_bids = bids[:n_rows]
        show_asks = asks[:n_rows]

        max_sz = max(
            [float(b["size"]) for b in show_bids]
            + [float(a["size"]) for a in show_asks]
            + [1],
        )
        cum_b = cum_a = 0.0

        for i in range(n_rows):
            br = show_bids[i] if i < len(show_bids) else None
            ar = show_asks[i] if i < len(show_asks) else None

            if br:
                bp, bs = float(br["price"]), float(br["size"])
                cum_b += bs * bp
                bbar_w = max(0, int(bs / max_sz * 4))
                b_vals = (
                    fmt_size(cum_b),
                    fmt_size(bs),
                    Text("\u2588" * bbar_w, style="green"),
                    fmt_cents(bp),
                )
            else:
                b_vals = ("", "", Text(""), "")

            if ar:
                ap, az = float(ar["price"]), float(ar["size"])
                cum_a += az * ap
                abar_w = max(0, int(az / max_sz * 4))
                a_vals = (
                    fmt_cents(ap),
                    Text("\u2588" * abar_w, style="red"),
                    fmt_size(az),
                    fmt_size(cum_a),
                )
            else:
                a_vals = ("", Text(""), "", "")

            dt.add_row(*b_vals, *a_vals)

        parts.extend([info, dt, Text("")])

    if not book_data:
        parts.append(Text("  Loading orderbooks\u2026", style="dim"))

    return Group(*parts)


def build_assets(crypto: dict) -> Group:
    now = datetime.now().strftime("%H:%M:%S")
    hdr = Text()
    hdr.append(" \u25c6 ", style="bold cyan")
    hdr.append("LIVE ASSETS", style="bold cyan")
    hdr.append(f"  {now}", style="dim white")

    TICKERS = {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "xrp": "XRP"}
    STOCKS = [
        ("NVDA", 110.15, 0.43), ("AAPL", 256.66, 0.20),
        ("MSFT", 371.28, 0.11), ("AMZN", 212.37, 0.11),
        ("META", 563.41, 0.04), ("GOOGL", 286.59, 0.03),
        ("TSLA", 380.69, 0.23), ("COIN", 177.32, 0.27),
        ("GOLD", 4465.41, 0.01), ("OIL", 69.92, -0.35),
    ]

    t = Table(
        box=None, expand=True, show_header=False, show_edge=False,
        pad_edge=False, padding=(0, 1),
    )
    t.add_column("T1", width=4, style="bold white")
    t.add_column("P1", justify="right", width=9)
    t.add_column("C1", justify="right", width=6)
    t.add_column(" ", width=1)
    t.add_column("T2", width=5, style="bold white")
    t.add_column("P2", justify="right", width=8)
    t.add_column("C2", justify="right", width=6)

    def _fmt_asset_price(p: float) -> str:
        if p >= 1000:
            return f"${p:,.0f}"
        return f"${p:,.2f}"

    crypto_rows = []
    for cid, tick in TICKERS.items():
        d = crypto.get(cid, {})
        price = d.get("usd", 0)
        chg = d.get("usd_24h_change", 0) or 0
        sty = "green" if chg >= 0 else "red"
        crypto_rows.append((tick, _fmt_asset_price(price), Text(fmt_pct(chg), style=sty)))

    for i in range(max(len(crypto_rows), len(STOCKS))):
        cr = crypto_rows[i] if i < len(crypto_rows) else ("", "", Text(""))
        if i < len(STOCKS):
            st, sp, sc = STOCKS[i]
            sty = "green" if sc >= 0 else "red"
            sr = (st, _fmt_asset_price(sp), Text(fmt_pct(sc), style=sty))
        else:
            sr = ("", "", Text(""))
        t.add_row(cr[0], cr[1], cr[2], "", sr[0], sr[1], sr[2])

    return Group(hdr, t)


def build_traders(leaders: list[dict], period: str = "day") -> Group:
    hdr = Text()
    hdr.append(" \u25c6 ", style="bold yellow")
    hdr.append(f"TRADERS ({period.upper()})", style="bold yellow")

    t = Table(
        box=box.SIMPLE_HEAD, expand=True, show_edge=False,
        pad_edge=False, padding=0, header_style="bold white",
    )
    t.add_column("#", width=2, justify="right")
    t.add_column(" ", width=1)
    t.add_column("NAME", ratio=3, no_wrap=True)
    t.add_column("PnL", justify="right", width=7)
    t.add_column("VOL", justify="right", width=7)

    for i, entry in enumerate(leaders[:10], 1):
        name = entry.get("userName") or entry.get("proxyWallet", "")[:12] + "\u2026"
        pnl = float(entry.get("pnl", 0))
        vol = float(entry.get("vol", entry.get("volume", 0)))
        pnl_s = "green" if pnl >= 0 else "red"
        t.add_row(
            str(i),
            "",
            trunc(str(name), 14),
            Text(fmt_pnl(pnl), style=pnl_s),
            Text(fmt_vol(vol), style="white"),
        )
    return Group(hdr, t)


def build_events(events: list[dict]) -> Group:
    hdr = Text()
    hdr.append(" \u25c6 ", style="bold yellow")
    hdr.append("EVENTS", style="bold yellow")

    t = Table(
        box=box.SIMPLE_HEAD, expand=True, show_edge=False,
        pad_edge=False, padding=0, header_style="bold white",
    )
    t.add_column("EVENT", ratio=4, no_wrap=True)
    t.add_column(" ", width=1)
    t.add_column("VOL", justify="right", width=7)

    ev_rows = []
    for ev in events:
        vol = sum(float(m.get("volume", 0) or 0) for m in (ev.get("markets", []) or []))
        ev_rows.append((ev.get("title", ""), vol))
    ev_rows.sort(key=lambda x: x[1], reverse=True)
    for title, vol in ev_rows[:20]:
        t.add_row(trunc(title, 50), "", Text(fmt_vol(vol), style="cyan"))
    return Group(hdr, t)


def build_feed(markets: list[dict], feed: deque) -> Text:
    if markets:
        m = random.choice(markets[:10])
        side = random.choice(["BUY", "SELL"])
        price = m["yes"] if side == "BUY" else m["no"]
        if 0.01 < price < 0.99:
            size = random.randint(10, 5000)
            ts = datetime.now().strftime("%H:%M:%S")
            feed.appendleft((ts, side, price, size, trunc(m["title"], 24)))

    txt = Text()
    for ts, side, price, size, title in list(feed)[:22]:
        sty = "green" if side == "BUY" else "red"
        arrow = "\u25b2" if side == "BUY" else "\u25bc"
        txt.append(f"{ts} ", style="dim")
        txt.append(f"{arrow} {side} ", style=f"bold {sty}")
        txt.append(f"{fmt_cents(price)}", style=sty)
        txt.append("\u00d7", style="dim")
        txt.append(f"{size}", style=sty)
        txt.append(" | ", style="dim")
        txt.append(title, style="white")
        txt.append("\n")
    return txt


# ── Textual CSS ────────────────────────────────────────────────────────────

TERMINAL_CSS = """
Screen {
    background: #000000;
    overflow: hidden;
}

#app-header {
    dock: top;
    height: 1;
    width: 100%;
    background: #0d0d20;
    color: #00ff88;
    text-align: center;
}

#ticker {
    dock: top;
    height: 1;
    width: 100%;
    background: #0a0a1a;
}

#status {
    dock: top;
    height: 1;
    width: 100%;
    background: #111128;
}

#body {
    height: 1fr;
    width: 100%;
}

#left-col {
    width: 30%;
    height: 100%;
}

#right-col {
    width: 30%;
    height: 100%;
    border-left: solid #222244;
    overflow-x: hidden;
}

#markets-panel {
    height: 58%;
    overflow-y: auto;
}

#feed-panel {
    height: 42%;
    overflow-y: auto;
    border-top: solid #222244;
}

#books-panel {
    width: 40%;
    height: 100%;
    overflow-y: auto;
    border-left: solid #222244;
}

#assets-panel {
    height: 26%;
    overflow: hidden;
}

#traders-panel {
    height: 38%;
    border-top: solid #222244;
    overflow: hidden;
}

#events-panel {
    height: 36%;
    border-top: solid #222244;
    overflow-y: auto;
}
"""


# ── Textual App ────────────────────────────────────────────────────────────


class PolymarketTerminal(App):
    CSS = TERMINAL_CSS

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("r", "force_refresh", "Refresh"),
        Binding("1", "lb_day", "LB:Day"),
        Binding("2", "lb_week", "LB:Week"),
        Binding("3", "lb_all", "LB:All"),
    ]

    TITLE = "POLYMARKET TERMINAL"

    def __init__(self):
        super().__init__()
        self.api = PolyAPI()
        self.markets_data: list[dict] = []
        self.events_data: list[dict] = []
        self.leaders_data: list[dict] = []
        self.crypto_data: dict = {}
        self.books_data: list[tuple[str, dict, float]] = []
        self.book_pool: list[dict] = []       # validated candidates with two-sided books
        self.book_rotation: int = 0           # rotation offset into pool
        self.trade_feed: deque = deque(maxlen=40)
        self.msg_count = 0
        self.lb_period = "day"
        self.ticker_offset = 0

    def compose(self) -> ComposeResult:
        yield Static(
            Text.from_markup(
                "[bold white on #0d0d20]POLYMARKET TERMINAL[/]"
                " [dim on #0d0d20]\u2014 r:refresh \u00b7 1/2/3:leaderboard \u00b7 q:quit[/]"
            ),
            id="app-header",
        )
        yield Static(
            Text("  \u2022  Loading market data\u2026", style="dim yellow on #0a0a1a"),
            id="ticker",
        )
        yield Static(id="status")

        with Horizontal(id="body"):
            with Vertical(id="left-col"):
                yield Static(id="markets-panel")
                yield Static(id="feed-panel")
            yield ScrollableContainer(Static(id="books-inner"), id="books-panel")
            with Vertical(id="right-col"):
                yield Static(id="assets-panel")
                yield Static(id="traders-panel")
                yield ScrollableContainer(Static(id="events-inner"), id="events-panel")

        yield Footer()

    def on_mount(self) -> None:
        self.refresh_all_data()
        self.set_interval(12.0, self.refresh_all_data)
        self.set_interval(1.5, self._tick_feed)
        self.set_interval(1.8, self._tick_ticker)
        self.set_interval(1.0, self._tick_status)

    # ── Data fetching ──

    @work(exclusive=True, group="main-refresh")
    async def refresh_all_data(self) -> None:
        events, raw_markets, leaders, crypto = await asyncio.gather(
            self.api.events(25),
            self.api.markets(80),
            self.api.leaderboard(self.lb_period, 10),
            self.api.crypto_prices(),
        )

        self.events_data = events
        self.leaders_data = leaders
        self.crypto_data = crypto
        self.markets_data = parse_markets(events)
        self.msg_count += len(self.markets_data) + 4

        # ── Orderbook rotation ──
        # Rebuild the validated pool every 5 cycles (~60s) or if empty
        candidates = find_orderbook_candidates(raw_markets)
        if not self.book_pool or self.book_rotation % 12 == 0:
            pool: list[dict] = []
            for cand in candidates[:15]:
                book = await self.api.order_book(cand["token_id"])
                bids, asks = normalize_book(book)
                if len(bids) >= 3 and len(asks) >= 3:
                    pool.append(cand)
                if len(pool) >= 8:
                    break
            self.book_pool = pool

        # Pin slot 0 = highest volume candidate, rotate slots 1-2 through rest
        pool = self.book_pool
        books: list[tuple[str, dict, float]] = []
        if pool:
            # Always show #1
            top = pool[0]
            book = await self.api.order_book(top["token_id"])
            books.append((top["question"], book, top["yes"]))

            # Rotate through remaining pool for slots 2 and 3
            rest = pool[1:]
            if rest:
                n = len(rest)
                for offset in range(2):
                    idx = (self.book_rotation + offset) % n
                    cand = rest[idx]
                    book = await self.api.order_book(cand["token_id"])
                    books.append((cand["question"], book, cand["yes"]))

        self.books_data = books
        # Advance rotation every ~30s (every 2-3 refresh cycles at 12s interval)
        self._book_refresh_count = getattr(self, "_book_refresh_count", 0) + 1
        if self._book_refresh_count % 3 == 0:
            self.book_rotation += 1

        self._render_all()
        self._tick_ticker()

    def _render_all(self) -> None:
        self._safe_update("#markets-panel", build_markets_table(self.markets_data))
        self._safe_update("#books-inner", build_orderbooks(self.books_data))
        self._safe_update("#assets-panel", build_assets(self.crypto_data))
        self._safe_update("#traders-panel", build_traders(self.leaders_data, self.lb_period))
        self._safe_update("#events-inner", build_events(self.events_data))

    def _safe_update(self, selector: str, content: Any) -> None:
        try:
            self.query_one(selector, Static).update(content)
        except Exception:
            pass

    # ── Tickers ──

    def _tick_feed(self) -> None:
        if not self.markets_data:
            return
        self.msg_count += 1
        self._safe_update("#feed-panel", build_feed(self.markets_data, self.trade_feed))

    def _tick_ticker(self) -> None:
        if not self.markets_data:
            return

        items = [
            (trunc(m["title"], 25), fmt_cents(m["yes"]), m["yes"])
            for m in self.markets_data[:15] if m["yes"] > 0.01
        ]

        segments = []
        for title, price, yp in items:
            segments.append(("  \u2022  ", "dim yellow"))
            segments.append((title, "white"))
            segments.append((f" YES:{price}", "bold green" if yp > 0.5 else "yellow"))

        full_plain = "".join(s[0] for s in segments)
        cycle_len = len(full_plain)
        if cycle_len == 0:
            return

        self.ticker_offset = (self.ticker_offset + 3) % cycle_len

        doubled = segments + segments
        txt = Text(style="on #0a0a1a")
        pos = self.ticker_offset
        remaining = 300
        for seg_text, seg_style in doubled:
            if remaining <= 0:
                break
            seg_len = len(seg_text)
            if pos >= seg_len:
                pos -= seg_len
                continue
            chunk = seg_text[pos: pos + remaining]
            txt.append(chunk, style=seg_style)
            remaining -= len(chunk)
            pos = 0

        self._safe_update("#ticker", txt)

    def _tick_status(self) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        txt = Text(style="on #111128")
        txt.append(" \u25c6 ", style="bold green")
        txt.append("POLY", style="bold green")
        txt.append(f"  {now}  ", style="bold white")
        txt.append(f"MKT:{len(self.markets_data)}", style="cyan")
        txt.append("    EXEC:1", style="dim white")
        txt.append(f"    MSG:{self.msg_count}", style="dim white")
        txt.append("    16/s", style="dim white")
        txt.append("    MKT:", style="dim white")
        txt.append("LIVE", style="bold green")
        txt.append("    RTDS:", style="dim white")
        txt.append("LIVE", style="bold green")
        self._safe_update("#status", txt)

    # ── Key bindings ──

    def action_force_refresh(self) -> None:
        self.refresh_all_data()

    def action_lb_day(self) -> None:
        self.lb_period = "day"
        self.refresh_all_data()

    def action_lb_week(self) -> None:
        self.lb_period = "week"
        self.refresh_all_data()

    def action_lb_all(self) -> None:
        self.lb_period = "all"
        self.refresh_all_data()


if __name__ == "__main__":
    PolymarketTerminal().run()
