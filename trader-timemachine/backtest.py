#!/usr/bin/env python3
"""Backtest for trader-timemachine, NSE (India): the opening-range breakout,
sized and charged the way trader-budget and trader-risk would, on every stored
stock and session at once. Standard library only. It never places an order.

    python3 backtest.py collect                        # 60 days of 5-min bars for trader/universe.txt
    python3 backtest.py collect --symbols RELIANCE,TCS
    python3 backtest.py collect --offline RELIANCE-60d5m.json.gz --symbols RELIANCE   # a saved Yahoo chart
    python3 backtest.py run                            # every stored session, the config's rules
    python3 backtest.py run --from 2026-08-01 --write trader/backtest.json --trades trader/backtest-trades.csv

Why a store. Yahoo keeps 5-minute bars for 60 days, so one download is never a
year. collect merges each download into trader/bars/<SYMBOL>.json.gz; run it
every week or two and the history grows (a broker's history can go into the
same files in Phase 1). run says how many sessions it had and whether that is
the year the gate asks for.

A full session is the 72 five-minute bars of continuous trading, 9:15 to 15:15
IST (F&O stocks then go into the closing auction, which leaves those bars
empty); a day with a missing bar is left out and counted.

The rule (the same as trader-entry and trader-exit):
  range      the first --or-minutes (15) of the session: 9:15 to 9:30
  entry      the first 5-minute bar after the range to CLOSE above the range
             high, closing between no_entries_before and no_entries_after
             (09:30 and 14:45); filled at that close plus slippage
  stop       the range midpoint (--stop or-mid) or low (or-low), to the paisa
  target     the entry + --target-r (2) x the risk, to the paisa
  exit       on a later bar: the stop if the bar trades at or below it (at the
             bar's open if it opened below: a gap fills worse); else the target
             if the bar trades at or above it; a bar that touches both is the
             stop; else at flat_by (15:10), at that bar's open. Stop and flat-by
             exits pay slippage, the target (a limit order) does not.
Sizing and limits (trader/config.json, as trader-budget and trader-risk apply them):
  shares     the most that fit max_loss_per_trade_usd with the round trip's
             charges, the position cap, max_order_usd and the cash not in use
  skipped    reward:risk after charges under min_reward_risk, a stop under
             min_stop_pct of the price (when set), or zero shares
  a day      at most max_trades_per_day entries, first come first served across
             stocks, and no entry whose max loss with charges takes the day's
             total at risk past max_daily_loss_usd (each entry counts in full)
Charges are NSE intraday charges ("costs" in the config, Zerodha's rates by
default); slippage_pct (0.02 % a side by default) is extra. Capital does not
compound: every day starts from the budget. R = the trade's net result over its
max loss with charges, so a stop is about -1R.

Filters (the research report's, each off unless asked for):
  --nifty-vwap       the trigger bar must close with the market above its VWAP
                     since 9:15. Yahoo's Nifty index has no volume, so the market
                     is NIFTYBEES (the Nifty 50 ETF), whose bars collect stores
  --max-gap PCT      skip the stock that day when its 9:15 open is more than
                     PCT % from the previous session's last 5-minute close
  --min-volume-x X   the trigger bar's volume must be X times the average of the
                     opening-range bars that traded (Yahoo's 9:15 bar often shows
                     zero volume; those are left out of the average). As in
                     trader-entry, a thin breakout bar does not count and a later
                     bar can still trigger; so can a later bar for --nifty-vwap
  --vix-max V        India VIX's close the day before above V: --vix-mode half
                     (the default) halves the max loss a trade, skip skips the day

Not modelled: the opening auction, partial fills, the price band and the F&O
ban on past days (NSE does not publish them backwards), and news. A backtest
of past bars is not a forecast.

Phase 1's gate (Gate 2 of the roadmap), each line PASS or NOT YET:
  history         at least 250 sessions (a year)
  trades          at least 200 trades
  expectancy      positive after charges, over everything
  out of sample   positive after charges on the last --oos-sessions (63), never
                  more than a quarter of the sessions
  drawdown        the deepest fall of the equity curve within
                  gate_max_drawdown_pct (6, the monthly loss stop) of the budget

Exit codes: run 0 when the gate passes, 1 when it does not or it could not run.
"""

import argparse
import csv
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
HOSTS = ("query2", "query1")
PAUSE = 0.5           # seconds between requests: be polite to free endpoints
BACKOFF = 5.0         # seconds to wait once after a 429
STEP = 300            # 5-minute bars, in seconds
OPEN_MIN = 9 * 60 + 15
END_MIN = 15 * 60 + 15  # continuous trading ends; the closing auction leaves no 5-minute bars
SESSION_BARS = (END_MIN - OPEN_MIN) // 5   # 72
IST = datetime.timedelta(hours=5, minutes=30)
EPOCH = datetime.datetime(1970, 1, 1)
DEFAULT_STORE = "trader/bars"
MARKET_PROXY = "NIFTYBEES"   # the Nifty 50 ETF: the market's VWAP (the index itself has no volume)
VIX_FILE = "INDIAVIX.daily.json"   # India VIX daily closes; not a .json.gz, so run never trades it
DEFAULTS = {  # the India config's values (trader-budget/config.example.json); the config overrides them
    "budget": 500000, "cash_buffer_pct": 10, "max_position_pct": 40, "max_order_usd": 200000,
    "min_order_usd": 1000, "max_loss_per_trade_usd": 2500, "max_daily_loss_usd": 7500,
    "max_trades_per_day": 3, "min_reward_risk": 1.5, "no_entries_before": "09:30",
    "no_entries_after": "14:45", "flat_by": "15:10", "slippage_pct": 0.02, "min_stop_pct": 0,
    "gate_max_drawdown_pct": 6,
}
GATE_SESSIONS = 250
GATE_TRADES = 200
OOS_SESSIONS = 63
# NSE intraday equity charges, from Zerodha's published rates (zerodha.com/charges, read 2026-10-23);
# the same table and arithmetic as trader-budget/size.py and trader-risk/check.py.
COSTS_NSE = {
    "brokerage_pct": 0.03,     # per executed order, 0.03% of its value...
    "brokerage_max": 20.0,     # ...or Rs 20, whichever is lower
    "stt_sell_pct": 0.025,     # securities transaction tax, intraday: sell side only
    "exchange_pct": 0.00307,   # NSE transaction charge, both sides
    "sebi_per_crore": 10.0,    # SEBI turnover fee, Rs 10 a crore, both sides
    "stamp_buy_pct": 0.003,    # stamp duty, intraday: buy side only
    "gst_pct": 18.0,           # GST on brokerage + exchange + SEBI charges
}
SKIP_WORDS = {"rr": "reward:risk after charges under the minimum", "min_stop": "stop under min_stop_pct",
              "zero": "zero shares (the stop is too far for the max loss)", "small": "under min_order_usd",
              "trades": "the day's trades limit", "daily_loss": "the daily loss limit", "cash": "no cash free",
              "filter": "no bar passed the volume or Nifty filter", "gap": "opening gap over --max-gap",
              "vix": "India VIX over --vix-max", "no_market": "no NIFTYBEES bars that day",
              "no_prev": "no previous close for the gap"}


class BacktestError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def config_market(path):
    """The "market" in trader/config.json, or None."""
    try:
        with open(path, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("market")
    except (OSError, ValueError, AttributeError):
        return None


def ist_of(ts):
    """Unix seconds -> naive India time. Never uses the machine's time zone."""
    return EPOCH + datetime.timedelta(seconds=int(ts)) + IST


def hhmm_min(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise BacktestError("%s wants an India time like 14:45, not %r" % (what, text))
    return int(m.group(1)) * 60 + int(m.group(2))


def clock(minutes):
    return "%d:%02d" % divmod(minutes, 60)


def paisa(x):
    """Half up to the paisa."""
    return int(float(x) * 100 + 0.5 + 1e-9) / 100.0


def paisa_down(x):
    """A long's stop rounds down, away from the entry (as trader-exit does)."""
    return int(float(x) * 100 + 1e-9) / 100.0


def money(x):
    return ("-" if x < 0 else "") + "Rs " + "{:,.0f}".format(abs(x))


def intraday_charges(qty, buy, sell, rates=None):
    """Rupee charges for buying qty shares at `buy` and selling them at `sell` the
    same day on NSE: {brokerage, stt, exchange, sebi, stamp, gst, total}, to the paisa."""
    r = dict(COSTS_NSE, **(rates or {}))
    bv, sv = qty * float(buy), qty * float(sell)
    brokerage = sum(min(v * r["brokerage_pct"] / 100.0, r["brokerage_max"]) for v in (bv, sv) if v > 0)
    stt = sv * r["stt_sell_pct"] / 100.0
    exchange = (bv + sv) * r["exchange_pct"] / 100.0
    sebi = (bv + sv) * r["sebi_per_crore"] / 1e7
    stamp = bv * r["stamp_buy_pct"] / 100.0
    gst = (brokerage + exchange + sebi) * r["gst_pct"] / 100.0
    out = {k: round(v, 2) for k, v in (("brokerage", brokerage), ("stt", stt), ("exchange", exchange),
                                         ("sebi", sebi), ("stamp", stamp), ("gst", gst))}
    out["total"] = round(brokerage + stt + exchange + sebi + stamp + gst, 2)
    return out


def shares_within_loss(max_loss, entry, stop, rates=None):
    """The most whole shares whose loss at the stop plus the round trip's charges fits."""
    risk = entry - stop
    if risk <= 0 or max_loss <= 0:
        return 0
    lo, hi = 0, int(max_loss // risk)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if mid * risk + intraday_charges(mid, entry, stop, rates)["total"] <= max_loss + 0.005:
            lo = mid
        else:
            hi = mid - 1
    return lo


# ---------------------------------------------------------------- the bar store

def yahoo_symbol(sym):
    """RELIANCE -> RELIANCE.NS; an index (^INDIAVIX) stays as it is."""
    sym = sym.upper()
    return sym if sym.endswith(".NS") or sym.startswith("^") else sym + ".NS"


def store_path(store, sym):
    return os.path.join(store, "%s.json.gz" % re.sub(r"[^A-Za-z0-9&_-]", "_", sym.upper()))


def load_bars(store, sym):
    """{ts: [open, high, low, close, volume]} for one symbol, or {}."""
    path = store_path(store, sym)
    if not os.path.exists(path):
        return {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        doc = json.load(fh)
    return {int(k): v for k, v in (doc.get("bars") or {}).items()}


def save_bars(store, sym, bars):
    os.makedirs(store, exist_ok=True)
    path = store_path(store, sym)
    doc = {"symbol": sym.upper(), "interval": "5m", "zone": "IST", "source": "yahoo",
           "bars": {str(k): bars[k] for k in sorted(bars)}}
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    os.replace(tmp, path)


def chart_bars(text, sym):
    """A Yahoo v8 chart -> {ts: bar} of COMPLETE 5-minute bars of continuous
    trading (9:15 to 15:15 IST), on the grid, with prices."""
    try:
        res = json.loads(text)["chart"]["result"][0]
    except (ValueError, KeyError, TypeError, IndexError):
        raise BacktestError("Yahoo returned no chart for %s" % sym)
    meta = res.get("meta") or {}
    msym = str(meta.get("symbol") or "").upper()
    if msym and msym != yahoo_symbol(sym):
        raise BacktestError("the chart is for %s, not %s" % (msym, sym))
    if meta.get("dataGranularity") not in (None, "5m"):
        raise BacktestError("%s: %s bars, not 5m" % (sym, meta.get("dataGranularity")))
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    as_of = meta.get("regularMarketTime")
    out = {}
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c, v = [col[i] if i < len(col) else None for col in cols]
        if ts is None or None in (o, h, l, c) or int(ts) % STEP:
            continue
        local = ist_of(ts)
        mins = local.hour * 60 + local.minute
        if not OPEN_MIN <= mins < END_MIN:
            continue
        if as_of and int(ts) + STEP > int(as_of):
            continue   # still forming when Yahoo answered
        out[int(ts)] = [round(o, 2), round(h, 2), round(l, 2), round(c, 2), int(v or 0)]
    return out


def vix_closes(text):
    """A Yahoo daily chart of ^INDIAVIX -> {date: close}, the date in India time."""
    try:
        res = json.loads(text)["chart"]["result"][0]
    except (ValueError, KeyError, TypeError, IndexError):
        raise BacktestError("Yahoo returned no chart for ^INDIAVIX")
    closes = (((res.get("indicators") or {}).get("quote") or [{}])[0] or {}).get("close") or []
    return {ist_of(ts).date().isoformat(): round(c, 2)
            for ts, c in zip(res.get("timestamp") or [], closes) if ts is not None and c is not None}


def load_vix(store):
    path = os.path.join(store, VIX_FILE)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh).get("closes") or {}


def save_vix(store, closes):
    os.makedirs(store, exist_ok=True)
    with open(os.path.join(store, VIX_FILE), "w", encoding="utf-8") as fh:
        json.dump({"symbol": "^INDIAVIX", "interval": "1d", "source": "yahoo",
                   "closes": {d: closes[d] for d in sorted(closes)}}, fh, indent=0)


def fetch_chart(sym, sleep=time.sleep, rng="60d", interval="5m"):
    """One request: 60 days of 5-min bars by default; query2, then query1 on 429 or 5xx."""
    last = None
    for n, host in enumerate(HOSTS):
        if n:
            sleep(PAUSE)
        url = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=%s" % (
            host, urllib.parse.quote(yahoo_symbol(sym)), rng, interval)
        for attempt in (0, 1):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                last = e.code
                e.close()
                if e.code == 429 and attempt == 0:
                    sleep(BACKOFF)
                    continue
                if e.code == 429 or e.code >= 500:
                    break
                raise BacktestError("Yahoo answered HTTP %d for %s" % (e.code, sym))
            except (urllib.error.URLError, OSError) as e:
                raise BacktestError("could not reach Yahoo: %s" % getattr(e, "reason", e))
    raise BacktestError("Yahoo answered HTTP %s for %s on both hosts" % (last, sym))


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def symbols_from(a):
    if a.symbols:
        return [s for s in re.split(r"[,\s]+", a.symbols.upper()) if s]
    path = a.universe
    if not os.path.exists(path):
        raise BacktestError("no %s: run scan.py universe first, or pass --symbols" % path)
    with open(path, encoding="utf-8") as fh:
        return [ln.split("|")[0].strip().upper() for ln in fh if ln.strip() and not ln.startswith("#")]


# ---------------------------------------------------------------- sessions and the rule

def sessions(bars):
    """{date: [72 bars as dicts]} for the FULL sessions only, and the dates left out."""
    by_day = {}
    for ts in sorted(bars):
        local = ist_of(ts)
        slot = (local.hour * 60 + local.minute - OPEN_MIN) // 5
        o, h, l, c, v = bars[ts]
        by_day.setdefault(local.date().isoformat(), {})[slot] = {
            "slot": slot, "start": OPEN_MIN + 5 * slot, "o": o, "h": h, "l": l, "c": c, "v": v}
    full, short = {}, []
    for day, slots in by_day.items():
        if len(slots) == SESSION_BARS and all(k in slots for k in range(SESSION_BARS)):
            full[day] = [slots[k] for k in range(SESSION_BARS)]
        else:
            short.append(day)
    return full, sorted(short)


def with_vwap(day_bars):
    """The market proxy's bars with "above": close at or above the VWAP since 9:15
    (typical price (h + l + c) / 3, weighted by volume) at each bar's close."""
    pv = vol = 0.0
    out = []
    for b in day_bars:
        pv += (b["h"] + b["l"] + b["c"]) / 3.0 * b["v"]
        vol += b["v"]
        out.append(dict(b, above=(b["c"] >= pv / vol) if vol else None))
    return out


def signal(day_bars, rules, market=None):
    """The day's opening-range breakout on one stock, or None: the trigger bar,
    the entry (its close plus slippage), the stop, the target and the risk.
    With rules["min_volume_x"] or rules["nifty_vwap"] (market = the proxy's bars
    from with_vwap), a breakout bar that fails them does not count."""
    n_or = rules["or_minutes"] // 5
    rng = day_bars[:n_or]
    oh, ol = max(b["h"] for b in rng), min(b["l"] for b in rng)
    traded = [b["v"] for b in rng if b["v"]]
    avg_vol = sum(traded) / float(len(traded)) if traded else None
    for b in day_bars[n_or:]:
        closes_at = b["start"] + 5
        if closes_at > rules["last_entry_min"]:
            return None
        if rules.get("min_volume_x") and (not avg_vol or b["v"] < rules["min_volume_x"] * avg_vol):
            continue
        if rules.get("nifty_vwap") and not (market and market[b["slot"]]["above"]):
            continue
        if b["c"] > oh and closes_at >= rules["first_entry_min"]:
            entry = paisa(b["c"] * (1 + rules["slip"]))
            stop = paisa_down((oh + ol) / 2.0 if rules["stop"] == "or-mid" else ol)
            if stop >= entry:
                return None
            risk = round(entry - stop, 2)
            return {"slot": b["slot"], "time": clock(closes_at), "signal_close": b["c"], "entry": entry,
                    "stop": stop, "target": paisa(entry + rules["target_r"] * risk), "risk": risk,
                    "or_high": oh, "or_low": ol}
    return None


def exit_of(day_bars, sig, rules):
    """(price, time, reason) for a trade entered at the close of bar sig["slot"]."""
    stop, target, slip = sig["stop"], sig["target"], rules["slip"]
    for b in day_bars[sig["slot"] + 1:]:
        if b["start"] >= rules["flat_min"]:
            return paisa(b["o"] * (1 - slip)), clock(b["start"]), "flat"
        if b["l"] <= stop:
            return paisa(min(stop, b["o"]) * (1 - slip)), clock(b["start"]), "stop"
        if b["h"] >= target:
            return target, clock(b["start"]), "target"
    last = day_bars[-1]
    return paisa(last["c"] * (1 - slip)), clock(last["start"] + 5), "flat"


def size_trade(sig, cfg, cash_free, rates, loss_scale=1.0):
    """(qty, why_not, charges_at_stop, charges_at_target) as trader-budget sizes it.
    loss_scale 0.5 halves the max loss a trade (India VIX over --vix-max)."""
    entry, stop, target = sig["entry"], sig["stop"], sig["target"]
    if cfg["min_stop_pct"] and sig["risk"] < entry * float(cfg["min_stop_pct"]) / 100.0 - 1e-9:
        return 0, "min_stop", None, None
    budget = float(cfg["budget"])
    limits = [shares_within_loss(float(cfg["max_loss_per_trade_usd"]) * loss_scale, entry, stop, rates),
              int(budget * float(cfg["max_position_pct"]) / 100.0 // entry),
              int(float(cfg["max_order_usd"]) // entry), int(max(cash_free, 0) // entry)]
    qty = min(limits)
    if qty <= 0:
        return 0, ("cash" if limits[3] <= 0 < min(limits[:3]) else "zero"), None, None
    if qty * entry < float(cfg["min_order_usd"]) - 0.005:
        return 0, "small", None, None
    at_stop, at_target = intraday_charges(qty, entry, stop, rates), intraday_charges(qty, entry, target, rates)
    loss_all = qty * (entry - stop) + at_stop["total"]
    reward_net = qty * (target - entry) - at_target["total"]
    if float(cfg["min_reward_risk"]) and reward_net / loss_all < float(cfg["min_reward_risk"]) - 1e-9:
        return 0, "rr", at_stop, at_target
    return qty, None, at_stop, at_target


def prev_closes(bars):
    """{date: the previous session's last 5-minute close} from every stored bar."""
    last = {}
    for ts in sorted(bars):
        last[ist_of(ts).date().isoformat()] = bars[ts][3]
    days = sorted(last)
    return {d: last[days[i - 1]] for i, d in enumerate(days) if i}


def backtest(stores, cfg, rules, date_from=None, date_to=None, market=None, vix=None):
    """Pure function. stores = {SYM: {ts: bar}}. Every day, every stock's signal in
    time order, the day's limits, the sizing and the exit. Returns the trades,
    the skipped signals, the dates and the sessions each stock had.
    market = the market proxy's bars (NIFTYBEES), for --nifty-vwap; vix = {date:
    India VIX close}, for --vix-max (the close of the session before)."""
    rates = cfg.get("costs")
    days, left_out, prev = {}, {}, {}
    for sym, bars in stores.items():
        full, short = sessions(bars)
        left_out[sym] = short
        if rules.get("max_gap_pct") is not None:
            prev[sym] = prev_closes(bars)
        for day, day_bars in full.items():
            if (date_from and day < date_from) or (date_to and day > date_to):
                continue
            days.setdefault(day, {})[sym] = day_bars
    market_days = {d: with_vwap(b) for d, b in sessions(market)[0].items()} if market else {}
    vix_days = sorted(vix or {})
    investable = float(cfg["budget"]) * (1 - float(cfg["cash_buffer_pct"]) / 100.0)
    trades, skipped = [], []
    for day in sorted(days):
        loss_scale = 1.0
        if rules.get("vix_max") is not None:
            before = [d for d in vix_days if d < day]
            if before and vix[before[-1]] > rules["vix_max"]:
                if rules.get("vix_mode", "half") == "skip":
                    for sym in days[day]:
                        skipped.append({"date": day, "symbol": sym, "time": "", "why": "vix"})
                    continue
                loss_scale = 0.5
        sigs = []
        for sym, day_bars in days[day].items():
            if rules.get("nifty_vwap") and day not in market_days:
                skipped.append({"date": day, "symbol": sym, "time": "", "why": "no_market"})
                continue
            if rules.get("max_gap_pct") is not None:
                pc = prev[sym].get(day)
                if not pc:
                    skipped.append({"date": day, "symbol": sym, "time": "", "why": "no_prev"})
                    continue
                if abs(day_bars[0]["o"] / pc - 1) * 100 > rules["max_gap_pct"] + 1e-9:
                    skipped.append({"date": day, "symbol": sym, "time": "", "why": "gap"})
                    continue
            s = signal(day_bars, rules, market_days.get(day))
            if s:
                sigs.append((s["slot"], sym, s, day_bars))
            elif (rules.get("min_volume_x") or rules.get("nifty_vwap")) and signal(day_bars, dict(
                    rules, min_volume_x=None, nifty_vwap=False)):
                skipped.append({"date": day, "symbol": sym, "time": "", "why": "filter"})
        sigs.sort(key=lambda x: (x[0], x[1]))
        n, at_risk, open_pos = 0, 0.0, []   # open_pos: (exit slot, rupees in use)
        for slot, sym, s, day_bars in sigs:
            open_pos = [p for p in open_pos if p[0] > slot]
            if n >= int(cfg["max_trades_per_day"]):
                skipped.append({"date": day, "symbol": sym, "time": s["time"], "why": "trades"})
                continue
            qty, why, at_stop, at_target = size_trade(s, cfg, investable - sum(p[1] for p in open_pos), rates,
                                                      loss_scale)
            if not qty:
                skipped.append({"date": day, "symbol": sym, "time": s["time"], "why": why})
                continue
            max_loss = round(qty * (s["entry"] - s["stop"]) + at_stop["total"], 2)
            if at_risk + max_loss > float(cfg["max_daily_loss_usd"]) + 0.005:
                skipped.append({"date": day, "symbol": sym, "time": s["time"], "why": "daily_loss"})
                continue
            price, when, reason = exit_of(day_bars, s, rules)
            exit_slot = SESSION_BARS if reason == "flat" else (hhmm_min(when, "exit") - OPEN_MIN) // 5
            charges = intraday_charges(qty, s["entry"], price, rates)["total"]
            gross = round(qty * (price - s["entry"]), 2)
            net = round(gross - charges, 2)
            trades.append({"date": day, "symbol": sym, "entry_time": s["time"], "entry": s["entry"],
                           "stop": s["stop"], "target": s["target"], "qty": qty, "exit_time": when,
                           "exit": price, "reason": reason, "gross": gross, "charges": charges, "net": net,
                           "max_loss": max_loss, "r": round(net / max_loss, 3)})
            n += 1
            at_risk += max_loss
            open_pos.append((exit_slot, qty * s["entry"]))
    return {"trades": trades, "skipped": skipped, "dates": sorted(days),
            "sessions": {s: sum(1 for d in days if s in days[d]) for s in stores}, "left_out": left_out}


# ---------------------------------------------------------------- the numbers and the gate

def stats(trades, dates, budget):
    """The summary of a list of trades over the given dates (days with no trade count)."""
    nets = [t["net"] for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    by_day = {}
    for t in trades:
        by_day[t["date"]] = by_day.get(t["date"], 0.0) + t["net"]
    equity, peak, dd = 0.0, 0.0, 0.0
    for d in dates:
        equity += by_day.get(d, 0.0)
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    n = len(trades)
    return {"sessions": len(dates), "trades": n, "wins": len(wins),
            "win_rate": round(len(wins) / float(n), 3) if n else None,
            "net": round(sum(nets), 2), "gross": round(sum(t["gross"] for t in trades), 2),
            "charges": round(sum(t["charges"] for t in trades), 2),
            "avg_net": round(sum(nets) / n, 2) if n else None,
            "avg_r": round(sum(t["r"] for t in trades) / n, 3) if n else None,
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
            "max_drawdown": round(dd, 2), "max_drawdown_pct": round(100.0 * dd / budget, 2),
            "exits": {k: sum(1 for t in trades if t["reason"] == k) for k in ("target", "stop", "flat")}}


def oos_split(dates, want):
    """The last `want` sessions, never more than a quarter of them."""
    k = min(want, len(dates) // 4)
    return (dates[:-k], dates[-k:]) if k else (dates, [])


def gate(total, oos, cfg):
    """[(name, passed, words)] for Phase 1's gate."""
    lim = float(cfg["gate_max_drawdown_pct"])
    return [
        ("history", total["sessions"] >= GATE_SESSIONS,
         "%d sessions of history, the gate wants %d (a year)" % (total["sessions"], GATE_SESSIONS)),
        ("trades", total["trades"] >= GATE_TRADES,
         "%d trades, the gate wants %d" % (total["trades"], GATE_TRADES)),
        ("expectancy", bool(total["trades"]) and total["avg_net"] > 0,
         "average %s a trade after charges (%s R)" % (
             money(total["avg_net"] or 0), "%+.3f" % total["avg_r"] if total["trades"] else "no")),
        ("out of sample", bool(oos["trades"]) and oos["avg_net"] > 0,
         "the last %d sessions: %d trades, average %s after charges" % (
             oos["sessions"], oos["trades"], money(oos["avg_net"] or 0))),
        ("drawdown", total["max_drawdown_pct"] <= lim + 1e-9,
         "deepest fall %s (%.2f%% of the budget), the limit is %s%%" % (
             money(total["max_drawdown"]), total["max_drawdown_pct"], ("%g" % lim))),
    ]


def report(res, total, ins, oos, checks, cfg, rules):
    out = []
    dates = res["dates"]
    syms = sorted(s for s, n in res["sessions"].items() if n)
    out.append("Backtest: %d-minute opening-range breakout, stop at the range %s, %gR target, entries %s to %s, "
               "flat by %s (IST)." % (rules["or_minutes"], "midpoint" if rules["stop"] == "or-mid" else "low",
                                      rules["target_r"], clock(rules["first_entry_min"]),
                                      clock(rules["last_entry_min"]), clock(rules["flat_min"])))
    out.append("%d stocks, %d sessions (%s to %s); %s budget, %s max loss a trade, %d trades a day, NSE charges "
               "and %s%% slippage a side." % (len(syms), len(dates), dates[0] if dates else "-",
                                              dates[-1] if dates else "-", money(float(cfg["budget"])),
                                              money(float(cfg["max_loss_per_trade_usd"])),
                                              int(cfg["max_trades_per_day"]), "%g" % float(cfg["slippage_pct"])))
    filters = []
    if rules.get("nifty_vwap"):
        filters.append("the market (%s) above its VWAP" % MARKET_PROXY)
    if rules.get("max_gap_pct") is not None:
        filters.append("opening gap within %g%%" % rules["max_gap_pct"])
    if rules.get("min_volume_x"):
        filters.append("trigger volume %gx the range's" % rules["min_volume_x"])
    if rules.get("vix_max") is not None:
        vr = res.get("vix_range")
        filters.append("India VIX over %g the day before: %s%s" % (
            rules["vix_max"], "half the max loss" if rules.get("vix_mode", "half") == "half" else "no trades",
            " (VIX ranged %g to %g in this window)" % vr if vr else ""))
    out.append("Filters: %s." % ("; ".join(filters) if filters else "none"))
    out.append("")
    for name, s in (("All", total), ("In sample", ins), ("Out of sample", oos)):
        if not s["sessions"]:
            continue
        if not s["trades"]:
            out.append("%-14s %3d sessions, no trades." % (name, s["sessions"]))
            continue
        out.append("%-14s %3d sessions, %4d trades, win rate %4.1f%%, net %s (gross %s, charges %s), "
                   "average %s a trade, %+.3f R" % (
                       name, s["sessions"], s["trades"], 100.0 * s["win_rate"], money(s["net"]), money(s["gross"]),
                       money(s["charges"]), money(s["avg_net"]), s["avg_r"]))
    if total["trades"]:
        out.append("Exits: %d target, %d stop, %d flat by %s. Profit factor %s. Deepest fall %s (%.2f%% of the "
                   "budget)." % (total["exits"]["target"], total["exits"]["stop"], total["exits"]["flat"],
                                 clock(rules["flat_min"]), total["profit_factor"] if total["profit_factor"] is not None
                                 else "n/a", money(total["max_drawdown"]), total["max_drawdown_pct"]))
    if res["skipped"]:
        counts = {}
        for s in res["skipped"]:
            counts[s["why"]] = counts.get(s["why"], 0) + 1
        out.append("Signals not taken: " + ", ".join("%d %s" % (n, SKIP_WORDS.get(k, k))
                                                      for k, n in sorted(counts.items(), key=lambda kv: -kv[1])) + ".")
    mrr = float(cfg["min_reward_risk"] or 0)
    if mrr and rules["target_r"] <= mrr + 1e-9:
        out.append("Note: a %gR target can never reach min_reward_risk %g after charges, so every signal is "
                   "skipped: use a bigger --target-r, or a lower min_reward_risk in the config." % (
                       rules["target_r"], mrr))
    short = sum(len(v) for v in res["left_out"].values())
    if short:
        out.append("Left out %d stock-days with a missing bar (a halt, or a gap in Yahoo's data)." % short)
    out.append("")
    out.append("Phase 1 gate:")
    for name, ok, words in checks:
        out.append("  %-7s %-14s %s" % ("PASS" if ok else "NOT YET", name, words))
    verdict = all(ok for _, ok, _ in checks)
    out.append("Gate: %s." % ("PASS" if verdict else "NOT YET: no real money until every line passes"))
    out.append("Past bars, NSE charges and the rules above; not a forecast. The price band and F&O ban of past days "
               "are not modelled. Education and tooling, not financial advice.")
    return out


# ---------------------------------------------------------------- commands

def read_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except ValueError as e:
            raise BacktestError("%s is not valid JSON: %s" % (path, e))
        cfg.update({k: v for k, v in doc.items() if v is not None})
    return cfg


def cmd_collect(a):
    syms = symbols_from(a)
    if a.offline and len(syms) != 1:
        raise BacktestError("collect --offline FILE imports one chart: give --symbols with one ticker")
    added_total, failed = 0, []
    for i, sym in enumerate(syms):
        try:
            if a.offline:
                text = read_text(a.offline)
            else:
                if i and a.pause:
                    time.sleep(a.pause)
                text = fetch_chart(sym)
            new = chart_bars(text, sym)
        except BacktestError as e:
            failed.append("%s (%s)" % (sym, e))
            continue
        bars = load_bars(a.store, sym)
        before = len(bars)
        bars.update(new)
        save_bars(a.store, sym, bars)
        added_total += len(bars) - before
        full, _ = sessions(bars)
        if not a.quiet:
            print("%-12s %5d new bars, %4d stored sessions (%s to %s)" % (
                sym, len(bars) - before, len(full), min(full) if full else "-", max(full) if full else "-"))
    print("Stored in %s: %d new bars for %d of %d stocks.%s" % (
        a.store, added_total, len(syms) - len(failed), len(syms),
        (" Failed: " + "; ".join(failed[:10]) + (" ..." if len(failed) > 10 else "")) if failed else ""))
    if not a.offline and not a.no_context:
        # the filters' context: the market proxy's 5-minute bars, and a year of India VIX closes
        try:
            if MARKET_PROXY not in syms:
                time.sleep(a.pause)
                bars = load_bars(a.store, MARKET_PROXY)
                bars.update(chart_bars(fetch_chart(MARKET_PROXY), MARKET_PROXY))
                save_bars(a.store, MARKET_PROXY, bars)
            time.sleep(a.pause)
            vix = load_vix(a.store)
            vix.update(vix_closes(fetch_chart("^INDIAVIX", rng="1y", interval="1d")))
            save_vix(a.store, vix)
            print("Context: %s 5-minute bars (the market's VWAP) and %d India VIX closes." % (MARKET_PROXY, len(vix)))
        except BacktestError as e:
            print("Context not stored (%s): --nifty-vwap and --vix-max need it." % e)
    return 0 if len(failed) < len(syms) else 1


def cmd_run(a):
    cfg = read_config(a.config)
    rules = {"or_minutes": a.or_minutes, "stop": a.stop, "target_r": a.target_r,
             "slip": float(cfg["slippage_pct"]) / 100.0,
             "first_entry_min": hhmm_min(cfg["no_entries_before"], "no_entries_before"),
             "last_entry_min": hhmm_min(cfg["no_entries_after"], "no_entries_after"),
             "flat_min": hhmm_min(cfg["flat_by"], "flat_by"), "nifty_vwap": a.nifty_vwap,
             "max_gap_pct": a.max_gap, "min_volume_x": a.min_volume_x, "vix_max": a.vix_max,
             "vix_mode": a.vix_mode}
    if a.or_minutes < 5 or a.or_minutes % 5 or a.or_minutes > 120:
        raise BacktestError("--or-minutes wants a multiple of 5 from 5 to 120")
    if not os.path.isdir(a.store):
        raise BacktestError("no bar store at %s: run backtest.py collect first" % a.store)
    names = [f[:-len(".json.gz")] for f in os.listdir(a.store) if f.endswith(".json.gz")]
    names = [n for n in names if n != MARKET_PROXY]   # the market's yardstick, not a trade
    if a.symbols:
        names = [s for s in re.split(r"[,\s]+", a.symbols.upper()) if s]
    stores = {s: load_bars(a.store, s) for s in sorted(names)}
    stores = {s: b for s, b in stores.items() if b}
    if not stores:
        raise BacktestError("no stored bars in %s for those stocks" % a.store)
    market = load_bars(a.store, MARKET_PROXY) if a.nifty_vwap else None
    if a.nifty_vwap and not market:
        raise BacktestError("--nifty-vwap needs %s's bars in the store: run backtest.py collect" % MARKET_PROXY)
    vix = load_vix(a.store) if a.vix_max is not None else None
    if a.vix_max is not None and not vix:
        raise BacktestError("--vix-max needs India VIX closes in the store: run backtest.py collect")
    res = backtest(stores, cfg, rules, a.date_from, a.date_to, market, vix)
    if vix:
        window = [v for d, v in vix.items() if res["dates"] and res["dates"][0] <= d <= res["dates"][-1]]
        res["vix_range"] = (min(window), max(window)) if window else None
    if not res["dates"]:
        raise BacktestError("no full sessions in the store for that window")
    budget = float(cfg["budget"])
    ins_dates, oos_dates = oos_split(res["dates"], a.oos_sessions)
    total = stats(res["trades"], res["dates"], budget)
    ins_set, oos_set = set(ins_dates), set(oos_dates)
    ins = stats([t for t in res["trades"] if t["date"] in ins_set], ins_dates, budget)
    oos = stats([t for t in res["trades"] if t["date"] in oos_set], oos_dates, budget)
    checks = gate(total, oos, cfg)
    doc = {"created_by": "trader-timemachine backtest", "rules": dict(rules, slip_pct=float(cfg["slippage_pct"])),
           "vix_range": res.get("vix_range"),
           "all": total, "in_sample": ins, "out_of_sample": oos,
           "gate": [{"check": n, "pass": ok, "words": w} for n, ok, w in checks],
           "passed": all(ok for _, ok, _ in checks), "trades": res["trades"], "skipped": res["skipped"]}
    if a.write:
        d = os.path.dirname(a.write)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
    if a.trades:
        with open(a.trades, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            cols = ["date", "symbol", "entry_time", "entry", "stop", "target", "qty", "exit_time", "exit", "reason",
                    "gross", "charges", "net", "max_loss", "r"]
            w.writerow(cols)
            for t in res["trades"]:
                w.writerow([t[c] for c in cols])
    if a.json:
        print(json.dumps(dict(doc, trades=len(res["trades"]), skipped=len(res["skipped"])), indent=2))
    else:
        print("\n".join(report(res, total, ins, oos, checks, cfg, rules)))
        for path in (a.write, a.trades):
            if path:
                print("Wrote %s." % path)
    return 0 if doc["passed"] else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Backtest the opening-range breakout on NSE (trader-timemachine).")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("collect", help="add 60 days of 5-minute bars per stock to the store")
    p.add_argument("--symbols", help="comma-separated tickers instead of the universe")
    p.add_argument("--universe", default="trader/universe.txt", help="one SYMBOL|Name a line (scan.py universe)")
    p.add_argument("--store", default=DEFAULT_STORE)
    p.add_argument("--offline", metavar="FILE", help="import a saved Yahoo chart JSON (.json or .json.gz)")
    p.add_argument("--pause", type=float, default=PAUSE)
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--no-context", action="store_true", help="skip the NIFTYBEES bars and India VIX closes")
    p.add_argument("--config", default="trader/config.json")
    p = sub.add_parser("run", help="backtest every stored session")
    p.add_argument("--nifty-vwap", action="store_true", help="the market (NIFTYBEES) above its VWAP at the trigger")
    p.add_argument("--max-gap", type=float, metavar="PCT", help="skip a stock that opened more than PCT %% away")
    p.add_argument("--min-volume-x", type=float, metavar="X", help="trigger volume X times the range's average")
    p.add_argument("--vix-max", type=float, metavar="V", help="India VIX close the day before above V")
    p.add_argument("--vix-mode", choices=["half", "skip"], default="half", help="over --vix-max: half size or skip")
    p.add_argument("--store", default=DEFAULT_STORE)
    p.add_argument("--symbols", help="only these tickers (default: every stock in the store)")
    p.add_argument("--from", dest="date_from", help="first session, YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", help="last session, YYYY-MM-DD")
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--stop", choices=["or-mid", "or-low"], default="or-mid")
    p.add_argument("--target-r", type=float, default=2.0)
    p.add_argument("--oos-sessions", type=int, default=OOS_SESSIONS)
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--write", nargs="?", const="trader/backtest.json", metavar="PATH")
    p.add_argument("--trades", nargs="?", const="trader/backtest-trades.csv", metavar="PATH")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        market = config_market(a.config)
        if market not in (None, "in"):
            raise BacktestError("the backtest is for NSE (\"market\": \"in\"); the config says %r" % market)
        return {"collect": cmd_collect, "run": cmd_run}[a.cmd](a)
    except BacktestError as e:
        print("backtest.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
