"""The India fork, Phase 0: the NSE clock, the NSE calendar, the NSE universe and
Indian intraday charges, every skill loaded on the "in" market.

Fixtures (tests/fixtures/trader-india) are real data, cut on 2026-10-23: NSE's own
EQUITY_L.csv, fo_mktlots.csv and the Nifty 200 list (trimmed to a few rows), and
RELIANCE's 5-minute bars from Yahoo (RELIANCE.NS): 5 days and 60 days. The reel
for India is Monday 2026-10-19, the same day as the TSLA reel: RELIANCE closed
above its 9:15-9:30 opening range at 9:50 AM IST.
"""

import contextlib
import datetime
import gzip
import io
import json
import os
import tempfile
import unittest
from decimal import Decimal

from _load import FIX, ROOT, load

IN = os.path.join(FIX, "trader-india")
FIVE_DAYS = os.path.join(IN, "RELIANCE-5d5m.json")
SIXTY_DAYS = os.path.join(IN, "RELIANCE-60d5m.json.gz")
CFG = os.path.join(ROOT, "trader-budget", "config.example.json")
DT = datetime.datetime
D = datetime.date

scan = load("trader-scanner", "scan", market="in")
entry = load("trader-entry", "entry", market="in")
exit_ = load("trader-exit", "exit", market="in")
tm = load("trader-timemachine", "timemachine", market="in")
size = load("trader-budget", "size", market="in")
check = load("trader-risk", "check", market="in")
ticket = load("trader-broker", "ticket", market="in")
alerts = load("trader-alerts", "alerts", market="in")
news = load("trader-news", "news", market="in")
bt = load("trader-timemachine", "backtest", market="in")
CLOCKED = (scan, entry, exit_, tm, size, check, ticket, alerts)
CALENDARS = (scan, entry, exit_, check, alerts)

with open(CFG, encoding="utf-8") as fh:
    INDIA_CFG = json.load(fh)


def ist(text):
    return DT.strptime(text, "%Y-%m-%d %H:%M") - datetime.timedelta(hours=5, minutes=30)


def nse_lists(trade_date=None):
    """trader/nse.json built from NSE's real files of 2026-10-26 (trimmed), optionally re-dated."""
    texts = {}
    for f in scan.NSE_DAILY:
        with open(os.path.join(IN, f), encoding="utf-8") as fh:
            texts[f] = fh.read()
    doc = scan.build_nse_lists(texts, "2026-10-26T03:00:00Z", "2026-10-26")
    if trade_date:
        doc["trade_date"] = trade_date
    return doc


def quiet(fn, *args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = fn(*args)
    return code, out.getvalue()


class Clock(unittest.TestCase):
    def test_india_is_utc_plus_5_30_all_year(self):
        for mod in CLOCKED:
            self.assertEqual(mod.local_offset(DT(2026, 1, 15, 4, 0)), 5.5, mod.__name__)
            self.assertEqual(mod.local_offset(DT(2026, 7, 15, 4, 0)), 5.5, mod.__name__)   # no daylight saving
            self.assertEqual(mod.to_local(DT(2026, 10, 19, 3, 45)), DT(2026, 10, 19, 9, 15), mod.__name__)
            self.assertEqual(mod.local_to_utc(DT(2026, 10, 19, 9, 15)), DT(2026, 10, 19, 3, 45), mod.__name__)
            self.assertEqual(mod.parse_now("2026-10-19 09:50"), DT(2026, 10, 19, 4, 20), mod.__name__)

    def test_upstream_names_follow_the_market(self):
        first_bar = int((DT(2026, 10, 19, 3, 45) - DT(1970, 1, 1)).total_seconds())   # RELIANCE's 9:15 bar
        self.assertEqual(scan.ts_to_eastern(first_bar), DT(2026, 10, 19, 9, 15))
        self.assertEqual(entry.to_eastern(DT(2026, 10, 19, 4, 20)), DT(2026, 10, 19, 9, 50))

    def test_broker_log_stamps_carry_the_half_hour(self):
        self.assertEqual(ticket.ny_iso(DT(2026, 10, 19, 4, 20)), "2026-10-19T09:50:00+05:30")

    def test_news_clock(self):
        aware = DT(2026, 10, 19, 4, 20, tzinfo=datetime.timezone.utc)
        self.assertEqual(news.to_local(aware), DT(2026, 10, 19, 9, 50))
        self.assertEqual(news.ZONE, "IST")


class Calendar(unittest.TestCase):
    def test_the_same_nse_list_in_every_skill(self):
        for mod in CALENDARS:
            self.assertEqual(mod.NSE_HOLIDAYS, scan.NSE_HOLIDAYS, mod.__name__)
        self.assertEqual(len(scan.NSE_HOLIDAYS), 15)

    def test_holidays_weekends_and_the_election_day_that_traded(self):
        self.assertEqual(scan.trading_day(D(2026, 11, 10)), (False, "NSE holiday: Diwali Balipratipada"))
        self.assertEqual(scan.trading_day(D(2026, 10, 20)), (False, "NSE holiday: Dussehra"))
        self.assertEqual(scan.trading_day(D(2026, 11, 8)), (False, "weekend"))   # Muhurat Sunday
        self.assertEqual(scan.trading_day(D(2026, 1, 15)), (True, ""))           # Nifty traded that day
        self.assertEqual(scan.session(D(2026, 10, 19)), (datetime.time(9, 15), datetime.time(15, 30)))
        self.assertIsNone(scan.session(D(2026, 12, 25)))

    def test_the_calendar_matches_sixty_days_of_real_sessions(self):
        with gzip.open(SIXTY_DAYS, "rt", encoding="utf-8") as fh:
            stamps = json.load(fh)["chart"]["result"][0]["timestamp"]
        days = sorted({scan.ts_to_local(t).date() for t in stamps})
        d = days[0]
        while d <= days[-1]:
            traded = d in days
            self.assertEqual(scan.trading_day(d)[0], traded, d)
            d += datetime.timedelta(days=1)

    def test_market_clock(self):
        st = alerts.market_status(ist("2026-11-10 10:00"))
        self.assertFalse(st["open"])
        self.assertEqual(st["next_open"], "2026-11-11 09:15")
        st = alerts.market_status(ist("2026-10-19 09:20"))
        self.assertTrue(st["open"])
        self.assertEqual((st["opens"], st["closes"]), ("09:15", "15:30"))

    def test_the_entry_window(self):
        cfg = dict(check.DEFAULTS)
        self.assertEqual((cfg["no_entries_before"], cfg["no_entries_after"]), ("09:30", "14:45"))
        self.assertIn("no new entries before 09:30 India", check.window_check(DT(2026, 10, 19, 9, 20), cfg)[0][0])
        self.assertIn("no new entries after 14:45 India", check.window_check(DT(2026, 10, 19, 14, 50), cfg)[0][0])
        self.assertIn("NSE holiday: Diwali", check.window_check(DT(2026, 11, 10, 10, 0), cfg)[0][0])
        self.assertEqual(check.window_check(DT(2026, 10, 19, 9, 50), cfg)[0], [])


class Universe(unittest.TestCase):
    def texts(self):
        out = {}
        for f in ("EQUITY_L.csv", "fo_mktlots.csv", "ind_nifty200list.csv"):
            with open(os.path.join(IN, f), encoding="utf-8") as fh:
                out[f] = fh.read()
        return out

    def test_fo_stocks_skip_the_index_rows(self):
        fo = scan.fo_stocks(self.texts()["fo_mktlots.csv"])
        self.assertEqual(len(fo), 10)
        for idx in ("NIFTY", "BANKNIFTY", "FINNIFTY", "Symbol"):
            self.assertNotIn(idx, fo)
        self.assertIn("M&M", fo)

    def test_fo_universe_drops_trade_to_trade_and_non_fo(self):
        kept, counts, _ = scan.build_universe_nse(self.texts(), "fo")
        self.assertEqual([s for s, _ in kept], ["360ONE", "BAJAJ-AUTO", "HDFCBANK", "INFY", "ITC", "M&M",
                                                "RELIANCE", "SBIN", "TCS", "TRENT"])
        self.assertEqual(dict(kept)["M&M"], "Mahindra & Mahindra Limited")
        self.assertEqual(counts["series BE (trade-to-trade)"], 3)
        self.assertEqual(counts["series BZ (trade-to-trade)"], 2)
        self.assertEqual(counts["not in the F&O list"], 5)
        self.assertEqual(counts["rows"], 20)

    def test_nifty200_and_all(self):
        self.assertEqual(len(scan.build_universe_nse(self.texts(), "nifty200")[0]), 12)
        self.assertEqual(len(scan.build_universe_nse(self.texts(), "all")[0]), 15)

    def test_universe_command_offline(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "universe.txt")
            code, text = quiet(scan.main, ["universe", "--offline", IN, "--out", out, "--market", "in"])
            self.assertEqual(code, 0)
            with open(out, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        self.assertIn("Kept 10 NSE stocks", text)
        self.assertIn("BAJAJ-AUTO|Bajaj Auto Limited", lines)

    def test_nse_symbols_on_yahoo(self):
        self.assertTrue(all(scan.NSE_TICKER.match(s) for s in ("RELIANCE", "M&M", "BAJAJ-AUTO", "360ONE")))
        self.assertFalse(scan.NSE_TICKER.match("BRK.B"))
        self.assertEqual(scan.to_yahoo("M&M"), "M&M.NS")
        self.assertEqual(scan.from_yahoo("BAJAJ-AUTO.NS"), "BAJAJ-AUTO")   # the hyphen stays
        self.assertIn("/v8/finance/chart/M%26M.NS?", scan.chart_path("M&M", "1d", "5m"))
        self.assertEqual(entry.yahoo_symbol("reliance"), "RELIANCE.NS")
        self.assertEqual(news.yahoo_symbol("M&M"), "M%26M.NS")
        self.assertIn("region=IN&lang=en-IN", news.rss_url("RELIANCE"))


class Entry(unittest.TestCase):
    def chart(self):
        return entry.get_chart("RELIANCE", "5m", offline=FIVE_DAYS)

    def test_the_opening_range_is_9_15_to_9_30(self):
        res = entry.evaluate(self.chart(), ist("2026-10-19 09:50"))
        self.assertEqual(res["symbol"], "RELIANCE")
        self.assertEqual(res["opening_range"]["high"], 1223.4)
        self.assertEqual(res["opening_range"]["low"], 1207.3)
        # the same range straight from the raw bars, at UTC+5:30
        with open(FIVE_DAYS, encoding="utf-8") as fh:
            r = json.load(fh)["chart"]["result"][0]
        q = r["indicators"]["quote"][0]
        rng = [i for i, t in enumerate(r["timestamp"])
               if DT(1970, 1, 1) + datetime.timedelta(seconds=t + 19800) in
               (DT(2026, 10, 19, 9, 15), DT(2026, 10, 19, 9, 20), DT(2026, 10, 19, 9, 25))]
        self.assertEqual(len(rng), 3)
        self.assertEqual(round(max(q["high"][i] for i in rng), 2), 1223.4)
        self.assertEqual(round(min(q["low"][i] for i in rng), 2), 1207.3)

    def test_the_reel_triggers_at_9_50(self):
        res = entry.evaluate(self.chart(), ist("2026-10-19 09:50"))
        self.assertEqual(res["status"], "triggered")
        self.assertEqual(res["trigger"]["time"], "9:50 AM")
        self.assertEqual(res["trigger"]["price"], 1227.8)
        self.assertEqual(res["trigger"]["bar_index"], 6)
        self.assertIn("TRIGGERED 9:50 AM at Rs 1227.80", "\n".join(entry.describe(res)))

    def test_expired_at_14_45_and_not_open_at_9_10(self):
        res = entry.evaluate(self.chart(), ist("2026-10-22 15:00"))
        self.assertEqual(res["status"], "expired")
        self.assertEqual(res["no_entries_after"], "14:45")
        res = entry.evaluate(self.chart(), ist("2026-10-22 09:10"))
        self.assertEqual(res["phase"], "pre-open")
        self.assertIn("the NSE opens at 9:15", res["reason"])


class Exit(unittest.TestCase):
    def test_or_mid_stop_2r_target_in_rupees(self):
        c = exit_.compute(1227.80, "or_mid", None, "r", Decimal("2"), 1223.40, 1207.30)
        self.assertEqual((c["stop"], c["target"], c["risk"]), (Decimal("1215.35"), Decimal("1252.70"),
                                                               Decimal("12.45")))
        self.assertEqual(exit_.money(1227.8), "Rs 1,227.80")
        self.assertEqual(exit_.D("Rs 1,227.80"), Decimal("1227.80"))

    def test_flat_by_15_10_and_the_market_in_the_plan(self):
        self.assertEqual(exit_.DEFAULTS["flat_by"], "15:10")
        with tempfile.TemporaryDirectory() as d:
            plan = os.path.join(d, "plan.json")
            code, text = quiet(exit_.main, ["plan", "--symbol", "RELIANCE", "--entry", "1227.80", "--or-high",
                                            "1223.40", "--or-low", "1207.30", "--date", "2026-10-19", "--time",
                                            "9:50 AM", "--now", "2026-10-19 09:50", "--config", CFG,
                                            "--write", plan])
            self.assertEqual(code, 0)
            with open(plan, encoding="utf-8") as fh:
                doc = json.load(fh)
        self.assertEqual((doc["flat_by"], doc["market"]), ("15:10", "in"))
        self.assertIn("Flat by       15:10 IST", text)


class TimeMachine(unittest.TestCase):
    def test_a_full_nse_session_is_72_bars_to_15_15(self):
        self.assertEqual((tm.OPEN_MIN, tm.CLOSE_MIN, tm.SESSION_BARS), (555, 915, 72))

    def test_the_reel_replayed_on_real_nse_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "tm.json")
            code, _ = quiet(tm.main, ["run", "--symbol", "RELIANCE", "--date", "2026-10-19", "--entry", "1227.80",
                                      "--stop", "1215.35", "--target", "1252.70", "--entry-time", "9:50 AM",
                                      "--offline", SIXTY_DAYS, "--now", "2026-10-19 09:50", "--until", "15:10",
                                      "--market", "in", "--write", out])
            self.assertEqual(code, 0)
            with open(out, encoding="utf-8") as fh:
                doc = json.load(fh)
        mc, hist = doc["mc"], doc["history"]
        self.assertEqual((mc["pool_sessions"], mc["pool_from"], mc["pool_to"]), (55, "2026-07-29", "2026-10-16"))
        self.assertEqual((mc["p_target"], mc["p_stop"], mc["p_neither"]), (0.006, 0.1728, 0.8212))
        self.assertEqual((hist["triggered"], hist["target"], hist["stop"], hist["close"]), (26, 9, 11, 6))
        self.assertTrue(doc["verdict"].startswith("Strongly toward the stop"))


class Charges(unittest.TestCase):
    def test_the_worked_example(self):
        # buy 160 at 1,250 (Rs 2,00,000), stopped out at 1,240
        self.assertEqual(size.intraday_charges(160, 1250, 1240),
                         {"brokerage": 40.0, "stt": 49.6, "exchange": 12.23, "sebi": 0.4, "stamp": 6.0,
                          "gst": 9.47, "total": 117.7})

    def test_small_orders_pay_0_03_percent_not_rs_20(self):
        c = size.intraday_charges(10, 500, 495)   # 0.03% of Rs 5,000 and of Rs 4,950: 1.50 + 1.485
        self.assertAlmostEqual(c["brokerage"], 2.985, delta=0.006)

    def test_the_risk_gate_charges_the_same(self):
        for args in ((160, 1250, 1240), (162, 1227.8, 1215.35), (1, 3000, 2950), (2000, 95.5, 94.1)):
            self.assertEqual(check.intraday_charges(*args), size.intraday_charges(*args), args)

    def test_another_brokers_rates(self):
        zero = size.intraday_charges(160, 1250, 1240, {"brokerage_max": 0})
        self.assertEqual(zero["brokerage"], 0)
        self.assertEqual(zero["total"], round(117.7 - 40 - 40 * 0.18, 2))


class Sizing(unittest.TestCase):
    PLAN = {"symbol": "RELIANCE", "side": "buy", "date": "2026-10-23", "entry": 1250, "stop": 1240,
            "target": 1268, "setup": "orb"}

    def test_the_reports_example(self):
        s = size.size_trade(self.PLAN, INDIA_CFG, today="2026-10-23")
        self.assertEqual((s["qty"], s["bound_by"], s["refusal"]), (160, "position cap", None))
        ch = s["charges"]
        self.assertEqual(ch["at_stop"]["total"], 117.7)
        self.assertEqual(ch["max_loss_with_charges"], 1717.7)
        self.assertEqual(ch["reward_risk_net"], 1.61)
        order = size.bracket_order(s)
        self.assertEqual(order["plan"]["max_loss_with_charges"], 1717.7)
        self.assertIn("+ Rs 117.70 charges", order["why"])

    def test_the_max_loss_counts_the_charges(self):
        cfg = dict(INDIA_CFG, max_loss_per_trade_usd=1000, max_position_pct=100, max_order_usd=10 ** 7)
        s = size.size_trade(self.PLAN, cfg, today="2026-10-23")
        self.assertEqual((s["qty"], s["bound_by"]), (91, "max loss"))   # 100 shares without the charges
        self.assertLessEqual(91 * 10 + size.intraday_charges(91, 1250, 1240)["total"], 1000)
        self.assertGreater(92 * 10 + size.intraday_charges(92, 1250, 1240)["total"], 1000)

    def test_reward_risk_is_judged_after_charges(self):
        s = size.size_trade(dict(self.PLAN, target=1265), INDIA_CFG, today="2026-10-23")   # 1.5R before charges
        self.assertIn("reward:risk after charges 1.33", s["refusal"])
        self.assertIsNone(size.bracket_order(s))


class Risk(unittest.TestCase):
    NSE = nse_lists("2026-10-19")

    def order(self, qty):
        return {"symbol": "RELIANCE", "side": "buy", "qty": str(qty), "type": "market", "time_in_force": "day",
                "order_class": "bracket", "take_profit_limit_price": "1252.70", "stop_loss_stop_price": "1215.35",
                "plan": {"entry": 1227.8, "stop": 1215.35, "target": 1252.7}}

    def test_pass_with_charges_in_the_max_loss(self):
        res = check.check([self.order(162)], INDIA_CFG, now=ist("2026-10-19 09:50"), nse=self.NSE)
        r = res["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertIn("max loss Rs 2,134.09 with Rs 117.19 charges", " ".join(r["info"]))
        self.assertIn("reward:risk 1.83 after charges", " ".join(r["info"]))

    def test_the_charges_can_tip_an_order_over_the_limit(self):
        cfg = dict(INDIA_CFG, max_position_pct=100, max_order_usd=10 ** 7)
        res = check.check([self.order(200)], cfg, now=ist("2026-10-19 09:50"), nse=self.NSE)   # 200 x 12.45 = 2,490
        r = res["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any(x.startswith("loss: 200 shares x 12.45 risk a share = Rs 2,490 at the stop, plus")
                            for x in r["reasons"]), r["reasons"])

    def test_nse_symbols_pass_the_asset_check(self):
        o = dict(self.order(1), symbol="M&M")
        r = check.check([o], INDIA_CFG, now=ist("2026-10-19 09:50"), nse=self.NSE)["results"][0]
        self.assertFalse([x for x in r["reasons"] if x.startswith("asset")])
        r = check.check([dict(self.order(1), symbol="BRK.B")], INDIA_CFG, now=ist("2026-10-19 09:50"),
                        nse=self.NSE)["results"][0]
        self.assertTrue([x for x in r["reasons"] if "NSE stock" in x])


class NseLists(unittest.TestCase):
    """Price bands, the F&O ban and ASM/GSM, from NSE's real files for 2026-10-26."""

    def test_the_lists(self):
        doc = nse_lists()
        self.assertEqual(doc["trade_date"], "2026-10-26")   # from the F&O ban file's own header
        self.assertEqual(doc["fo_ban"], ["AMBUJACEM", "KAYNES", "SAIL"])
        self.assertIsNone(doc["bands"]["RELIANCE"])          # F&O stock: a dynamic band
        self.assertEqual((doc["bands"]["20MICRONS"], doc["bands"]["21STCENMGM"]), (20.0, 5.0))
        self.assertEqual(doc["series"]["AGSTRA"], "BZ")
        self.assertIn("long-term ASM", doc["asm"]["21STCENMGM"])
        self.assertIn("GSM", doc["gsm"]["AGSTRA"])
        self.assertEqual(doc["missing"], [])

    def test_command_offline(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "nse.json")
            code, text = quiet(scan.main, ["nse-lists", "--offline", IN, "--out", out, "--market", "in",
                                           "--now", "2026-10-26 08:45"])
            self.assertEqual(code, 0)
            with open(out, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["fo_ban"], ["AMBUJACEM", "KAYNES", "SAIL"])
        self.assertIn("F&O ban today: AMBUJACEM, KAYNES, SAIL.", text)

    def test_a_missing_file_is_said_not_hidden(self):
        with open(os.path.join(IN, "sec_list.csv"), encoding="utf-8") as fh:
            doc = scan.build_nse_lists({"sec_list.csv": fh.read()}, "2026-10-26T03:00:00Z", "2026-10-26")
        self.assertEqual(doc["missing"], ["fo_secban.csv", "asm.json", "gsm.json"])

    def run1(self, sym, entry, stop, target, qty=100, cfg=None, nse="today", prev=None):
        o = {"symbol": sym, "side": "buy", "qty": str(qty), "type": "market", "time_in_force": "day",
             "order_class": "bracket", "take_profit_limit_price": "%.2f" % target, "stop_loss_stop_price": "%.2f" % stop,
             "plan": {"entry": entry, "stop": stop, "target": target}}
        lists = nse_lists() if nse == "today" else nse
        res = check.check([o], cfg or INDIA_CFG, now=ist("2026-10-26 10:00"), nse=lists, prev_closes=prev or {})
        return res["results"][0]

    def blocked(self, r, start):
        return [x for x in r["reasons"] if x.startswith(start)]

    def test_no_lists_or_old_lists_block_entries(self):
        r = self.run1("RELIANCE", 1200, 1188, 1224, qty=10, nse=None)
        self.assertTrue(self.blocked(r, "nse lists: no trader/nse.json"), r["reasons"])
        r = self.run1("RELIANCE", 1200, 1188, 1224, qty=10, nse=nse_lists("2026-10-23"))
        self.assertTrue(self.blocked(r, "nse lists: trader/nse.json is for 2026-10-23, not today"), r["reasons"])
        cfg = dict(INDIA_CFG, require_nse_lists=False)
        r = self.run1("RELIANCE", 1200, 1188, 1224, qty=10, cfg=cfg, nse=None)
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertTrue([w for w in r["warnings"] if w.startswith("nse lists")])

    def test_fo_ban_and_surveillance(self):
        r = self.run1("AMBUJACEM", 560, 552, 576)
        self.assertTrue(self.blocked(r, "F&O ban: AMBUJACEM"), r["reasons"])
        r = self.run1("AMBUJACEM", 560, 552, 576, cfg=dict(INDIA_CFG, block_fo_ban=False))
        self.assertFalse(self.blocked(r, "F&O ban"))
        self.assertTrue([w for w in r["warnings"] if w.startswith("F&O ban")])
        r = self.run1("21STCENMGM", 100, 98, 104, prev={"21STCENMGM": 100})
        self.assertTrue(self.blocked(r, "surveillance: 21STCENMGM is under ASM"), r["reasons"])
        self.assertTrue(self.blocked(r, "price band: 21STCENMGM has a 5% band"), r["reasons"])
        r = self.run1("AGSTRA", 10, 9.9, 10.2, prev={"AGSTRA": 10})
        self.assertTrue(self.blocked(r, "series: AGSTRA trades in series BZ"), r["reasons"])
        self.assertTrue(self.blocked(r, "surveillance: AGSTRA is under GSM"), r["reasons"])

    def test_the_band_against_the_plan(self):
        prev = {"20MICRONS": 100}   # a 20 % band: 80.00 to 120.00
        r = self.run1("20MICRONS", 104, 100, 112, prev=prev)
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        r = self.run1("20MICRONS", 110, 105, 125, prev=prev)
        self.assertTrue(self.blocked(r, "price band: the target 125.00 is past today's band 120.00"), r["reasons"])
        r = self.run1("20MICRONS", 117, 112, 119.5, prev=prev)   # 117 is within 3 % of 120
        self.assertTrue(self.blocked(r, "price band: the entry 117.00 is within 3% of the band 120.00"), r["reasons"])
        r = self.run1("20MICRONS", 84, 79, 95, prev=prev)
        self.assertTrue(self.blocked(r, "price band: the stop 79.00 is past today's band 80.00"), r["reasons"])
        r = self.run1("20MICRONS", 104, 100, 112)   # no previous close: said, not blocked
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertTrue([w for w in r["warnings"] if w.startswith("price band: no previous close")])

    def test_exits_are_never_blocked_by_the_lists(self):
        o = {"symbol": "AMBUJACEM", "side": "sell", "qty": "10", "type": "market", "time_in_force": "day"}
        pf = {"cash": 100000, "positions": [{"ticker": "AMBUJACEM", "shares": 10}]}
        r = check.check([o], INDIA_CFG, portfolio=pf, now=ist("2026-10-26 15:05"), nse=None)["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])


class Broker(unittest.TestCase):
    def test_the_daily_loss_log_counts_the_charges(self):
        o = {"symbol": "RELIANCE", "side": "buy", "qty": "162", "type": "market", "stop_loss_stop_price": "1215.35",
             "plan": {"entry": 1227.8, "max_loss": 2016.9, "max_loss_with_charges": 2134.09}}
        self.assertEqual(ticket.max_loss(o), (2134.09, 1215.35))
        self.assertEqual(ticket._money(2134.09), "Rs 2,134.09")

    def test_manual_broker_places_nothing(self):
        o = {"symbol": "RELIANCE", "side": "buy", "qty": "162", "type": "market", "time_in_force": "day",
             "order_class": "bracket", "take_profit_limit_price": "1252.70", "stop_loss_stop_price": "1215.35"}
        text, _ = ticket.render(o, 1, 1, {"broker": "manual", "paper": True}, None, None, "2026-10-19")
        self.assertIn("no broker connected (you place it)", text)
        self.assertNotIn("place_stock_order", text)


class Alerts(unittest.TestCase):
    def test_flat_by_alert_defaults_to_15_10(self):
        rules = alerts.rules_from_plan({"symbol": "RELIANCE", "stop": 1215.35, "target": 1252.7,
                                        "date": "2026-10-19"})
        self.assertEqual(rules[-1]["at"], "15:10")

    def test_stooq_is_us_only(self):
        with self.assertRaises(alerts.AlertError):
            alerts.stooq_url("RELIANCE")


class Backtest(unittest.TestCase):
    """The Phase 1 backtest on RELIANCE's 60 days of real 5-minute bars."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = os.path.join(cls.tmp.name, "bars")
        code, cls.collected = quiet(bt.main, ["collect", "--offline", SIXTY_DAYS, "--symbols", "RELIANCE",
                                              "--store", cls.store, "--config", CFG])
        assert code == 0, cls.collected
        cls.bars = bt.load_bars(cls.store, "RELIANCE")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def rules(self, **kw):
        r = {"or_minutes": 15, "stop": "or-mid", "target_r": 2.0, "slip": 0.0, "first_entry_min": 570,
             "last_entry_min": 885, "flat_min": 910}
        r.update(kw)
        return r

    def test_the_store_keeps_full_sessions_and_merges(self):
        full, short = bt.sessions(self.bars)
        self.assertEqual((len(full), short), (59, ["2026-10-15"]))
        self.assertTrue(all(len(b) == 72 for b in full.values()))
        self.assertIn("59 stored sessions (2026-07-29 to 2026-10-23)", self.collected)
        code, text = quiet(bt.main, ["collect", "--offline", SIXTY_DAYS, "--symbols", "RELIANCE",
                                     "--store", self.store, "--config", CFG])
        self.assertIn("0 new bars", text)   # the same download twice adds nothing

    def test_the_reel_day_against_the_raw_bars(self):
        cfg = dict(INDIA_CFG, slippage_pct=0)
        res = bt.backtest({"RELIANCE": self.bars}, dict(bt.DEFAULTS, **cfg), self.rules(),
                          "2026-10-19", "2026-10-19")
        t = res["trades"][0]
        self.assertEqual((t["entry_time"], t["entry"], t["stop"], t["target"], t["qty"]),
                         ("9:50", 1227.8, 1215.35, 1252.7, 162))   # as entry, exit and budget said
        # the exit, found again straight from the bars
        day = bt.sessions(self.bars)[0]["2026-10-19"]
        want = None
        for b in day[7:]:
            if b["start"] >= 910:
                want = (b["o"], "flat")
                break
            if b["l"] <= 1215.35:
                want = (min(1215.35, b["o"]), "stop")
                break
            if b["h"] >= 1252.7:
                want = (1252.7, "target")
                break
        self.assertEqual((t["exit"], t["reason"]), want)
        charges = bt.intraday_charges(162, 1227.8, t["exit"])["total"]
        self.assertEqual(t["net"], round(162 * (t["exit"] - 1227.8) - charges, 2))

    def test_the_numbers_add_up(self):
        cfg = dict(bt.DEFAULTS, **INDIA_CFG)
        res = bt.backtest({"RELIANCE": self.bars}, cfg, self.rules(slip=0.0002))
        s = bt.stats(res["trades"], res["dates"], 500000)
        self.assertEqual(s["trades"], len(res["trades"]))
        self.assertAlmostEqual(s["net"], sum(t["net"] for t in res["trades"]), places=2)
        self.assertAlmostEqual(s["net"], s["gross"] - s["charges"], places=2)
        self.assertEqual(sum(s["exits"].values()), s["trades"])
        self.assertTrue(all(t["max_loss"] <= 2500 for t in res["trades"]))
        self.assertTrue(all(t["qty"] * t["entry"] <= 200000 for t in res["trades"]))

    def test_one_trade_a_day_across_stocks(self):
        cfg = dict(bt.DEFAULTS, **dict(INDIA_CFG, max_trades_per_day=1))
        res = bt.backtest({"RELIANCE": self.bars, "TWIN": self.bars}, cfg, self.rules())
        per_day = {}
        for t in res["trades"]:
            per_day[t["date"]] = per_day.get(t["date"], 0) + 1
        self.assertEqual(max(per_day.values()), 1)
        self.assertTrue(all(t["symbol"] == "RELIANCE" for t in res["trades"]))   # first come, ties by name
        self.assertTrue(any(s["why"] == "trades" and s["symbol"] == "TWIN" for s in res["skipped"]))

    def test_the_gate_is_not_yet_on_sixty_days(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "bt.json")
            code, text = quiet(bt.main, ["run", "--store", self.store, "--config", CFG, "--write", out])
            with open(out, encoding="utf-8") as fh:
                doc = json.load(fh)
        self.assertEqual(code, 1)
        self.assertFalse(doc["passed"])
        gate = {g["check"]: g["pass"] for g in doc["gate"]}
        self.assertEqual((gate["history"], gate["trades"]), (False, False))
        self.assertIn("59 sessions of history, the gate wants 250 (a year)", text)
        self.assertEqual(doc["out_of_sample"]["sessions"], 14)   # a quarter of 59, not 63

    def test_a_target_that_cannot_clear_the_minimum_is_explained(self):
        code, text = quiet(bt.main, ["run", "--store", self.store, "--config", CFG, "--target-r", "1.5"])
        self.assertIn("59 sessions, no trades.", text)
        self.assertIn("a 1.5R target can never reach min_reward_risk 1.5 after charges", text)

    def with_context(self):
        """The store plus the filters' context: NIFTYBEES's stored bars and India VIX closes (real, 2026-10-26)."""
        import shutil
        shutil.copy(os.path.join(IN, "NIFTYBEES-store.json.gz"), bt.store_path(self.store, "NIFTYBEES"))
        shutil.copy(os.path.join(IN, "INDIAVIX.daily.json"), os.path.join(self.store, bt.VIX_FILE))
        return bt.load_bars(self.store, "NIFTYBEES"), bt.load_vix(self.store)

    def test_vwap_is_volume_weighted_typical_price(self):
        bars = [{"h": 11, "l": 9, "c": 10, "v": 100}, {"h": 13, "l": 11, "c": 12, "v": 300},
                {"h": 12, "l": 10, "c": 10.5, "v": 0}]
        flags = [b["above"] for b in bt.with_vwap(bars)]
        self.assertEqual(flags, [True, True, False])   # VWAP 10, then 11.5, then still 11.5

    def test_volume_and_market_filters_only_take_bars_that_pass(self):
        market, _ = self.with_context()
        mdays = {d: bt.with_vwap(b) for d, b in bt.sessions(market)[0].items()}
        full, _ = bt.sessions(self.bars)
        checked = 0
        for day, bars in full.items():
            base = bt.signal(bars, self.rules())
            vol = bt.signal(bars, self.rules(min_volume_x=1.5))
            if vol:
                traded = [b["v"] for b in bars[:3] if b["v"]]
                self.assertGreaterEqual(bars[vol["slot"]]["v"], 1.5 * sum(traded) / len(traded))
                self.assertGreaterEqual(vol["slot"], base["slot"])
                checked += 1
            if day in mdays:
                mkt = bt.signal(bars, self.rules(nifty_vwap=True), mdays[day])
                if mkt:
                    self.assertTrue(mdays[day][mkt["slot"]]["above"])
                    self.assertGreaterEqual(mkt["slot"], base["slot"])
        self.assertGreater(checked, 0)

    def test_gap_uses_the_previous_sessions_last_close(self):
        prev = bt.prev_closes(self.bars)
        last_1016 = max(ts for ts in self.bars if bt.ist_of(ts).date().isoformat() == "2026-10-16")
        self.assertEqual(prev["2026-10-19"], self.bars[last_1016][3])
        cfg = dict(bt.DEFAULTS, **INDIA_CFG)
        res = bt.backtest({"RELIANCE": self.bars}, cfg, self.rules(max_gap_pct=0.5))
        full, _ = bt.sessions(self.bars)
        gapped = sum(1 for d, b in full.items() if d in prev and abs(b[0]["o"] / prev[d] - 1) * 100 > 0.5)
        self.assertEqual(sum(1 for s in res["skipped"] if s["why"] == "gap"), gapped)
        self.assertTrue(all(abs(full[t["date"]][0]["o"] / prev[t["date"]] - 1) * 100 <= 0.5 for t in res["trades"]))

    def test_vix_halves_the_size_or_skips_the_day(self):
        _, vix = self.with_context()
        cfg = dict(bt.DEFAULTS, **INDIA_CFG)
        half = bt.backtest({"RELIANCE": self.bars}, cfg, self.rules(vix_max=0), vix=vix)
        self.assertTrue(half["trades"])
        self.assertTrue(all(t["max_loss"] <= 1250 for t in half["trades"]))   # Rs 2,500 halved
        skip = bt.backtest({"RELIANCE": self.bars}, cfg, self.rules(vix_max=0, vix_mode="skip"), vix=vix)
        self.assertEqual(skip["trades"], [])
        self.assertTrue(all(s["why"] == "vix" for s in skip["skipped"]))

    def test_the_filters_are_named_in_the_report(self):
        self.with_context()
        code, text = quiet(bt.main, ["run", "--store", self.store, "--config", CFG, "--nifty-vwap", "--max-gap",
                                     "2", "--min-volume-x", "1.5", "--vix-max", "20"])
        self.assertIn("Filters: the market (NIFTYBEES) above its VWAP; opening gap within 2%; trigger volume 1.5x "
                      "the range's; India VIX over 20 the day before: half the max loss (VIX ranged", text)
        self.assertIn("1 stocks", text)   # NIFTYBEES is the yardstick, never a trade

    def test_a_chart_for_another_stock_is_refused(self):
        with gzip.open(SIXTY_DAYS, "rt", encoding="utf-8") as fh:
            text = fh.read()
        with self.assertRaises(bt.BacktestError):
            bt.chart_bars(text, "TCS")


class Pipeline(unittest.TestCase):
    def test_the_reel_from_entry_to_ticket(self):
        with tempfile.TemporaryDirectory() as d:
            p = lambda name: os.path.join(d, name)   # noqa: E731
            now = ["--now", "2026-10-19 09:50"]
            with open(p("nse.json"), "w", encoding="utf-8") as fh:
                json.dump(nse_lists("2026-10-19"), fh)
            self.assertEqual(quiet(entry.main, ["check", "RELIANCE", "--offline", FIVE_DAYS, "--config", CFG,
                                                "--write", p("entry.json")] + now)[0], 0)
            self.assertEqual(quiet(exit_.main, ["plan", "--from", p("entry.json"), "--config", CFG,
                                                "--write", p("plan.json")] + now)[0], 0)
            self.assertEqual(quiet(size.main, ["--plan", p("plan.json"), "--config", CFG, "--portfolio", p("none"),
                                               "--write", p("orders.json")] + now)[0], 0)
            code, text = quiet(check.main, ["--orders", p("orders.json"), "--config", CFG, "--portfolio", p("none"),
                                            "--prices", p("none.json"), "--state", p("broker.json"),
                                            "--nse", p("nse.json"), "--out", p("risk.json")] + now)
            self.assertEqual(code, 0, text)
            code, text = quiet(ticket.main, ["show", "--orders", p("orders.json"), "--config", CFG,
                                             "--risk", p("risk.json"), "--state", p("broker.json")] + now)
            with open(p("orders.json"), encoding="utf-8") as fh:
                order = json.load(fh)["orders"][0]
        self.assertEqual((order["qty"], order["stop_loss_stop_price"], order["take_profit_limit_price"]),
                         ("162", "1215.35", "1252.70"))
        self.assertEqual(order["plan"]["max_loss_with_charges"], 2134.09)
        self.assertIn("READY: trader-risk PASS", text)
        self.assertIn("max loss about Rs 2,134.09 with charges", text)


if __name__ == "__main__":
    unittest.main()
