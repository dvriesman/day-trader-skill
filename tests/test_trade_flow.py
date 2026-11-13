"""The whole trade on real data: PETR4, Thursday 2026-11-12 (the README's example)."""

import json
import os
import unittest

from _load import InTemp, PETR4, load, run

entry = load("trader-entry", "entry")
exitp = load("trader-exit", "exit")
tm = load("trader-timemachine", "timemachine")
size = load("trader-budget", "size")
check = load("trader-risk", "check")
ticket = load("trader-broker", "ticket")
alerts = load("trader-alerts", "alerts")


def rd(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class Entry(InTemp, unittest.TestCase):
    def test_waiting_while_the_bar_forms(self):
        code, out, _ = run(entry, ["check", "PETR4", "--now", "2026-11-12 10:22", "--offline", PETR4])
        self.assertEqual(code, 1)
        self.assertIn("Opening range 10:00-10:15: high R$ 44,09, low R$ 43,23 (3 of 3 bars).", out)
        self.assertIn("WAITING", out)
        self.assertIn("The 10:20 bar is still forming", out)

    def test_triggers_at_10_25(self):
        code, out, _ = run(entry, ["check", "PETR4", "--now", "2026-11-12 10:25", "--offline", PETR4, "--write"])
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED 10:25 at R$ 44,10", out)
        doc = rd("trader/entry.json")
        self.assertEqual((doc["price"], doc["time"], doc["bar_index"]), (44.1, "10:25", 4))
        self.assertEqual(doc["opening_range"], {"high": 44.09, "low": 43.23, "minutes": 15})

    def test_expires_after_the_window_and_knows_the_weekend(self):
        code, out, _ = run(entry, ["check", "PETR4", "--now", "2026-11-11 12:00", "--offline", PETR4])
        self.assertIn("EXPIRED", out)
        code, out, _ = run(entry, ["check", "PETR4", "--now", "2026-11-15 10:30", "--offline", PETR4])
        self.assertIn("B3 is closed today (weekend)", out)
        self.assertIn("Next open: 2026-11-16 10:00", out)

    def test_watch_replays_bar_by_bar(self):
        code, out, _ = run(entry, ["watch", "PETR4", "--now", "2026-11-12 10:00", "--offline", PETR4,
                                   "--out", "trader/entry.json"])
        self.assertEqual(code, 0)
        self.assertIn("10:10 bar  closed R$ 44,06  opening range set: high R$ 44,09, low R$ 43,23", out)
        self.assertIn("10:20 bar  closed R$ 44,10  TRIGGERED", out)

    def test_refuses_a_non_b3_ticker(self):
        code, _, err = run(entry, ["check", "AAPL", "--offline", PETR4])
        self.assertEqual(code, 1)
        self.assertIn("not a B3 ticker", err)


class Flow(InTemp, unittest.TestCase):
    def setUp(self):
        super().setUp()
        run(entry, ["check", "PETR4", "--now", "2026-11-12 10:25", "--offline", PETR4, "--write"])

    def plan(self):
        code, out, err = run(exitp, ["plan", "--stop", "or-mid", "--target", "1R", "--now", "2026-11-12 10:25",
                                     "--write"])
        self.assertEqual(code, 0, err)
        return out

    def test_exit_plan(self):
        out = self.plan()
        p = rd("trader/plan.json")
        self.assertEqual((p["entry"], p["stop"], p["target"], p["risk_per_share"], p["flat_by"]),
                         (44.1, 43.66, 44.54, 0.44, "11:45"))
        self.assertIn("Flat by       11:45 Sao Paulo", out)

    def test_exit_refuses_a_stop_inside_two_ticks(self):
        code, _, err = run(exitp, ["plan", "--symbol", "PETR4", "--entry", "44.10", "--stop", "price:44.09",
                                   "--date", "2026-11-12", "--now", "2026-11-12 10:25"])
        self.assertEqual(code, 1)
        self.assertIn("2 ticks", err)

    def test_flat_by_moves_inside_the_short_session(self):
        code, out, _ = run(exitp, ["plan", "--symbol", "PETR4", "--entry", "44.10", "--stop", "pct:1",
                                   "--date", "2026-06-10", "--now", "2026-06-10 10:25", "--config", "x.json"])
        code, _, err = run(exitp, ["plan", "--symbol", "PETR4", "--entry", "44.10", "--stop", "pct:1",
                                   "--flat-by", "17:00", "--date", "2026-06-10", "--now", "2026-06-10 10:25"])
        self.assertIn("16:55", err)

    def test_time_machine_numbers(self):
        self.plan()
        doc = tm.time_machine(tm.read_offline(PETR4, "PETR4"), tm.normalize_plan(rd("trader/plan.json")),
                              tm.parse_now("2026-11-12 10:25"))
        m, h = doc["mc"], doc["history"]
        self.assertEqual((m["p_target"], m["p_stop"], m["p_neither"]), (0.3572, 0.2936, 0.3492))
        self.assertEqual((m["pool_sessions"], m["until"]), (53, "11:45"))
        self.assertEqual((h["triggered"], h["target"], h["stop"], h["close"]), (32, 18, 9, 5))
        self.assertTrue(doc["verdict"].startswith("Close to a coin flip"))
        self.assertGreater(m["first_bar_rebuilt"], 0)

    def test_budget_sizes_in_lots_with_fees(self):
        self.plan()
        code, out, err = run(size, ["--now", "2026-11-12 10:25", "--write"])
        self.assertEqual(code, 0, err)
        o = rd("trader/orders.json")["orders"][0]
        self.assertEqual((o["symbol"], o["qty"], o["stop_loss_stop_price"], o["stop_loss_limit_price"],
                          o["take_profit_limit_price"]), ("PETR4", "200", "43.66", "43.44", "44.54"))
        self.assertEqual(o["plan"]["max_loss"], 93.27)
        self.assertIn("214 fit; B3's standard lot is 100, so 200", out)

    def test_small_budget_goes_fractional(self):
        self.plan()
        code, out, _ = run(size, ["--now", "2026-11-12 10:25", "--budget", "3000"])
        self.assertIn("51 PETR4F (fractional)", out)

    def test_a_live_xp_price_above_the_plan_shrinks_nothing_silently(self):
        self.plan()
        run(alerts, ["set-price", "PETR4", "44,12", "--now", "2026-11-12 10:31"])
        code, out, _ = run(size, ["--now", "2026-11-12 10:32"])
        self.assertIn("is above the plan's entry R$ 44,10, so the risk is bigger: sized at R$ 44,12", out)

    def test_usd_config_keys_are_refused(self):
        with open("trader/config.json", "w") as fh:
            json.dump({"budget": 5000, "max_loss_per_trade_usd": 50}, fh)
        self.plan()
        code, _, err = run(size, ["--now", "2026-11-12 10:25"])
        self.assertIn("_brl", err)

    def risk_and_tickets(self, check_now="2026-11-12 10:26"):
        self.plan()
        run(size, ["--now", "2026-11-12 10:25", "--write"])
        return run(check, ["--now", check_now])

    def test_risk_pass_inside_the_window_block_outside(self):
        code, out, _ = self.risk_and_tickets()
        self.assertEqual(code, 0)
        self.assertIn("max loss R$ 93,27 with fees", out)
        code, out, _ = run(check, ["--now", "2026-11-12 12:26", "--no-write"])
        self.assertEqual(code, 1)
        self.assertIn("no new entries after 11:30", out)
        code, out, _ = run(check, ["--now", "2026-11-20 10:30", "--no-write"])
        self.assertIn("B3 holiday", out)

    def test_pinned_risk_check_never_clears_a_real_order(self):
        self.risk_and_tickets()
        self.assertTrue(rd("trader/state/risk.json")["clock_pinned"])
        code, out, _ = run(ticket, ["show"])          # real clock
        self.assertEqual(code, 1)
        self.assertIn("NOT READY: the last risk check was a replay", out)
        code, out, _ = run(ticket, ["show", "--now", "2026-11-12 10:27"])   # a replay may use it
        self.assertEqual(code, 0)
        self.assertIn("READY", out)
        self.assertIn("REPLAY", out)

    def test_stale_risk_check_is_not_ready(self):
        self.risk_and_tickets()
        risk = rd("trader/state/risk.json")
        risk["clock_pinned"] = False
        risk["checked_at"] = "2026-11-12T13:00:00Z"
        with open("trader/state/risk.json", "w") as fh:
            json.dump(risk, fh)
        rv, why = ticket.risk_verdict(risk, "x", "2026-11-12", ticket.parse_now("2026-11-12 10:40"))
        self.assertIsNone(rv)
        self.assertIn("minutes old", why)

    def test_the_yes_is_strict_and_the_order_is_typed_by_the_user(self):
        self.risk_and_tickets()
        now = ["--now", "2026-11-12 10:27"]
        for reply in ("ok", "pode", "sim, todas", "sim mas espera", "sim VALE3", "sim 300", "yes?"):
            code, out, _ = run(ticket, ["approve", "--index", "1", "--reply", reply] + now)
            self.assertEqual(code, 1, reply)
            self.assertIn("Not recorded", out)
        code, out, _ = run(ticket, ["approve", "--index", "1", "--reply", "sim, 200 PETR4 a 44,10"] + now)
        self.assertEqual(code, 0)
        self.assertIn("Type it into your broker", out)
        self.assertIn("disparo 43,66, limite 43,44", out)
        code, out, _ = run(ticket, ["result", "--index", "1", "--status", "placed", "--broker-id", "777",
                                    "--filled-qty", "200", "--filled-price", "44,11", "--now", "2026-11-12 10:28"])
        pf = rd("trader/portfolio.json")
        self.assertEqual((pf["cash"], pf["positions"][0]["shares"]), (11178.0, 200.0))

    def test_flatten_splits_lots_and_fractional(self):
        code, out, _ = run(ticket, ["flatten", "--symbol", "PETR4", "--qty", "251", "--now", "2026-11-12 11:45"])
        orders = rd("trader/orders.json")["orders"]
        self.assertEqual([(o["symbol"], o["qty"]) for o in orders], [("PETR4", "200"), ("PETR4F", "51")])


class Risk(unittest.TestCase):
    def test_lots_options_shorts_and_foreign_tickers_block(self):
        orders = [{"symbol": "PETR4", "side": "buy", "qty": "150", "type": "market", "time_in_force": "day"},
                  {"symbol": "PETRK440", "side": "buy", "qty": "100", "type": "market", "time_in_force": "day"},
                  {"symbol": "PETR4F", "side": "sell", "qty": "10", "type": "market", "time_in_force": "day"},
                  {"symbol": "AAPL", "side": "buy", "qty": "1", "type": "market", "time_in_force": "day"},
                  {"symbol": "PETR4F", "side": "buy", "qty": "150", "type": "market", "time_in_force": "day"}]
        res = check.check(orders, {"budget": 20000}, now=check.parse_now("2026-11-12 10:26"))
        reasons = [" ".join(r["reasons"]) for r in res["results"]]
        self.assertIn("not a whole number of lots", reasons[0])
        self.assertIn("is an option", reasons[1])
        self.assertIn("short:", reasons[2])
        self.assertIn("not a B3 cash-market ticker", reasons[3])
        self.assertIn("fractional market takes 1 to 99", reasons[4])

    def test_a_fractional_sell_closes_a_standard_lot_buy(self):
        pf = {"cash": 1000, "positions": [{"ticker": "PETR4", "shares": 200}]}
        o = [{"symbol": "PETR4F", "side": "sell", "qty": "50", "type": "market", "time_in_force": "day"}]
        res = check.check(o, {}, portfolio=pf, prices={"PETR4": 44.0}, now=check.parse_now("2026-11-12 16:30"))
        self.assertEqual((res["results"][0]["kind"], res["results"][0]["verdict"]), ("exit", "PASS"))


class Alerts(InTemp, unittest.TestCase):
    def test_clock(self):
        code, out, _ = run(alerts, ["clock", "--now", "2026-11-12 10:30"])
        self.assertEqual(code, 0)
        self.assertIn("continuous trading 10:00 to 17:55, closing call to 18:00", out)
        code, out, _ = run(alerts, ["clock", "--now", "2026-11-12 17:57"])
        self.assertIn("closing call", out)

    def test_plan_rules_ring_once(self):
        with open("trader/plan.json", "w") as fh:
            json.dump({"symbol": "PETR4", "stop": 43.66, "target": 44.54, "flat_by": "11:45",
                       "date": "2026-11-12"}, fh)
        run(alerts, ["add", "--from-plan"])
        fx = os.path.join(os.path.dirname(PETR4))
        code, out, _ = run(alerts, ["check", "--now", "2026-11-12 11:46", "--offline", fx])
        self.assertIn("Flat by: it is 11:46 Sao Paulo", out)

    def test_set_price_guards_against_a_misread(self):
        run(alerts, ["set-price", "PETR4", "44.12", "--now", "2026-11-12 10:31"])
        code, _, err = run(alerts, ["set-price", "PETR4", "60", "--now", "2026-11-12 10:32"])
        self.assertEqual(code, 1)
        self.assertIn("away from the last known", err)
        doc = rd("trader/prices.json")
        self.assertEqual((doc["prices"]["PETR4"], doc["detail"]["PETR4"]["source"]), (44.12, "xp-browser"))


if __name__ == "__main__":
    unittest.main()
