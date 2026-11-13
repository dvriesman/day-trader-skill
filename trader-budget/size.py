#!/usr/bin/env python3
"""Position sizing for trader-budget. Standard library only, no network.

Sizes ONE B3 day trade from trader/plan.json (written by trader-exit) to your
budget and your max loss per trade, then writes one order (entry, stop-loss,
take-profit) to trader/orders.json. It never places anything: trader-risk
checks the order and trader-broker asks you yes or no; you type the order
into your broker's platform yourself.

    python3 size.py                                  # trader/plan.json + trader/config.json
    python3 size.py --write                          # write trader/orders.json
    python3 size.py --limit                          # a limit order at the plan's entry
    python3 size.py --limit 44.10 --write            # a limit order at 44.10
    python3 size.py --now "2026-11-12 10:25" --json  # pin the clock (Sao Paulo time)

How many shares. Whole shares, then B3's lots:

    shares = floor of the smallest of
      max loss       max_loss_per_trade_brl / (risk per share + fees per share)
      position cap   budget x max_position_pct / entry
      max order      max_order_brl / entry
      cash           (budget minus the cash buffer) / entry
                     (and no more than the cash in trader/portfolio.json, if you have one)

    then lots: 100 shares or more round DOWN to whole standard lots of 100
    (PETR4); under 100, with "fractional": true, the order goes to the
    fractional market (PETR4F, 1 to 99 shares). One order, one ticker, never
    a split across both books.

Fees. "fees_pct" (default 0.03, per side) estimates B3's day-trade fees
(emolumentos and liquidacao) plus brokerage on each leg, as a percent of the
order's value. It is counted in the max loss. Check it against your own nota
de corretagem and set it in the config.

The stop. B3 stop orders are stop-limit: a trigger price ("preco de disparo")
and a limit ("preco limite"). The ticket carries both: the trigger is the
plan's stop; the limit sits "stop_limit_slippage_pct" (default 0.5) under it,
at least 2 ticks. In a fast fall the price can gap past the limit and the
stop does not fill at all: the max loss shown is at the trigger.

The entry it sizes at: a limit order's limit price; for a market order the
plan's entry, or the price in trader/prices.json if that is fresh (its quote
time under 20 minutes old: free B3 quotes are about 15 minutes late) and
higher, because a price that ran up since the plan means a bigger loss at the
same stop. Zero shares (the stop is too far away for your max loss) is a
plain refusal: no order is written. So is a reward:risk under min_reward_risk.
"""

import argparse
import datetime
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import b3  # noqa: E402

# the defaults of config.example.json. Missing keys fall back to these.
DEFAULTS = {
    "budget": None,
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order_brl": 15000,
    "min_order_brl": 50,
    "max_loss_per_trade_brl": 100,
    "max_daily_loss_brl": 300,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.0,
    "require_stop": True,
    "no_entries_before": "10:15",
    "no_entries_after": "11:30",
    "flat_by": "11:45",
    "fractional": True,
    "lot_size": 100,
    "fees_pct": 0.03,
    "stop_limit_slippage_pct": 0.5,
    "broker": "manual",
    "paper": True,
    "blocked": [],
    "allow_options": False,
    "allow_margin": False,
    "allow_short": False,
}
EPS = 1e-9
FRESH_MINUTES = 20
SETUP_WORDS = {"orb": "opening-range breakout", "flag": "flag", "bounce": "oversold bounce",
               "level": "close above a level"}


class SizeError(Exception):
    pass


# ---------------------------------------------------------------- inputs

def parse_now(text):
    try:
        return b3.parse_now(text)
    except ValueError as e:
        raise SizeError("--now %s" % e)


def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SizeError("no %s at %s" % (what, path))
    except json.JSONDecodeError as e:
        raise SizeError("%s is not valid JSON: %s" % (path, e))


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        doc = read_json(path, "config")
        if not isinstance(doc, dict):
            raise SizeError("%s is not a JSON object" % path)
        old = sorted(k for k in doc if k.endswith("_usd"))
        if old:
            raise SizeError("%s has US-dollar keys (%s): this pack trades B3 in reais, rename them to _brl"
                            % (path, ", ".join(old)))
        cfg.update(doc)
    return cfg


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def price_time(doc, sym):
    """When this symbol's price is from: the quote's own time (detail[sym].time), else when it
    was fetched (detail[sym].fetched), else the file's as_of."""
    det = ((doc or {}).get("detail") or {}).get(sym) or {}
    for key in ("time", "fetched"):
        t = b3.parse_utc(det.get(key))
        if t:
            return t
    return b3.parse_utc((doc or {}).get("as_of"))


def fresh_price(doc, sym, now_utc, minutes=FRESH_MINUTES):
    """(price, time_utc, fresh) for sym from a prices.json doc, or (None, None, False)."""
    if not isinstance(doc, dict):
        return None, None, False
    prices = doc.get("prices") or {}
    p = _f(prices.get(sym))
    if p is None and sym.endswith("F"):
        p = _f(prices.get(sym[:-1]))
        sym = sym[:-1]
    if p is None or p <= 0:
        return None, None, False
    t = price_time(doc, sym)
    if t is None:
        return p, None, False
    age = (now_utc - t).total_seconds()
    return p, t, -60 <= age <= minutes * 60


# ---------------------------------------------------------------- money helpers

def round_cent(x):
    """Round a price to the cent (half up)."""
    return math.floor(float(x) * 100 + 0.5 + EPS) / 100.0


def floor_cent(x):
    return math.floor(float(x) * 100 + EPS) / 100.0


def shares_for(brl, per_share):
    """Whole shares that fit in brl at per_share, in exact integer cents (per_share may
    carry fractions of a cent: the fees)."""
    u = int(round(float(brl) * 10000))
    p = int(round(float(per_share) * 10000))
    if p <= 0 or u <= 0:
        return 0
    return u // p


def _price(x):
    """43.66 -> "43.66": order prices travel as strings with a dot."""
    return "%.2f" % round_cent(x)


def _num(x):
    x = round(float(x), 2)
    if abs(x - round(x)) < 0.005:
        return str(int(round(x)))
    return "%.2f" % x


money = b3.brl


# ---------------------------------------------------------------- sizing

def stop_limit_for(stop, cfg):
    """The stop-limit's limit price: slippage_pct under the trigger, at least 2 ticks, rounded down."""
    pct = float(cfg.get("stop_limit_slippage_pct") or 0)
    gap = max(stop * pct / 100.0, 2 * b3.TICK)
    return max(floor_cent(stop - gap), b3.TICK)


def size_trade(plan, cfg, budget=None, limit=None, price=None, cash=None, today=None):
    """Pure function: one plan + your rules -> the sizing (a dict). qty may be 0.

    limit  the limit price for a limit order (None = market)
    price  a FRESH latest price; a market order is sized at the higher of it and the plan's entry
    cash   the cash in trader/portfolio.json, if there is one
    today  Sao Paulo date (YYYY-MM-DD); a plan for another day is refused"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    try:
        sym = b3.base(plan.get("symbol"))
    except ValueError as e:
        raise SizeError("the plan's symbol: %s (run trader-exit)" % e)
    side = str(plan.get("side") or "buy").strip().lower()
    if side != "buy":
        raise SizeError("this pack sizes long (buy) day trades only; the plan says %r" % side)
    if today and plan.get("date") and str(plan["date"]) != today:
        raise SizeError("the plan is for %s and today is %s in Sao Paulo: make a new plan with trader-exit"
                        % (plan["date"], today))
    entry_plan, stop, target = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("entry", entry_plan), ("stop", stop), ("target", target)):
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s (run trader-exit); an order with a stop and a target "
                            "needs entry, stop and target" % name)
    blocked = {str(b).upper() for b in (c.get("blocked") or [])}
    if sym in blocked or b3.fractional(sym) in blocked:
        raise SizeError("%s is on your blocked list" % sym)

    budget = float(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json (in reais)")
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    lot = int(c.get("lot_size") or b3.LOT)
    fees_pct = float(c.get("fees_pct") or 0)

    if limit is not None:
        ref, ref_from = round_cent(limit), "limit"
    elif price is not None and round_cent(price) > round_cent(entry_plan):
        ref, ref_from = round_cent(price), "price"
    else:
        ref, ref_from = round_cent(entry_plan), "plan"
    stop, target = round_cent(stop), round_cent(target)
    if stop >= ref:
        raise SizeError("the stop %s is not below the entry %s; a long trade needs its stop underneath"
                        % (_price(stop), _price(ref)))
    if target <= ref:
        raise SizeError("the target %s is not above the entry %s: the move has already happened. "
                        "Skip it or make a new plan with trader-exit" % (_price(target), _price(ref)))
    risk_c = int(round((ref - stop) * 100))
    reward_c = int(round((target - ref) * 100))
    risk = risk_c / 100.0
    fee_ps = (ref + stop) * fees_pct / 100.0          # buy at the entry, sell at the stop
    loss_ps = risk + fee_ps

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    available = budget - cash_buffer
    cash_note = "budget %s minus the %s cash buffer" % (money(budget), money(cash_buffer))
    if cash is not None and cash - cash_buffer < available - 0.005:
        available = cash - cash_buffer
        cash_note = "cash %s in trader/portfolio.json minus the %s buffer" % (money(cash), money(cash_buffer))
    available = max(round(available, 2), 0.0)

    limits = []
    max_loss = _f(c.get("max_loss_per_trade_brl"))
    if max_loss:
        limits.append({"name": "max loss", "brl": max_loss, "per": loss_ps, "shares": shares_for(max_loss, loss_ps),
                       "how": "%s max loss / (%s risk + %s fees) a share" % (money(max_loss), _price(risk),
                                                                             "%.4f" % fee_ps)})
    cap_pct = _f(c.get("max_position_pct"))
    if cap_pct:
        cap = round(budget * cap_pct / 100.0, 2)
        limits.append({"name": "position cap", "brl": cap, "per": ref, "shares": shares_for(cap, ref),
                       "how": "%s%% of %s = %s / %s" % (_num(cap_pct), money(budget), money(cap), _price(ref))})
    max_order = _f(c.get("max_order_brl"))
    if max_order:
        limits.append({"name": "max order", "brl": max_order, "per": ref, "shares": shares_for(max_order, ref),
                       "how": "%s / %s" % (money(max_order), _price(ref))})
    limits.append({"name": "cash", "brl": available, "per": ref, "shares": shares_for(available, ref),
                   "how": "%s = %s / %s" % (cash_note, money(available), _price(ref))})

    raw_qty = max(0, min(l["shares"] for l in limits))
    bound = [l["name"] for l in limits if l["shares"] == raw_qty]
    for l in limits:
        l["binds"] = l["name"] == bound[0]
    lots, odd = b3.split_lots(raw_qty, lot)
    if lots:
        qty, book, ticker = lots, "standard lot", sym
    elif odd and c.get("fractional", True):
        qty, book, ticker = odd, "fractional", b3.fractional(sym)
    else:
        qty, book, ticker = 0, None, sym
    cost = round(qty * ref, 2)
    fees = round(qty * fee_ps, 2)
    loss = round(qty * risk_c / 100.0 + fees, 2)
    refusal = None
    if raw_qty == 0:
        if bound[0] == "max loss":
            refusal = ("one share risks %s (entry %s minus stop %s, plus fees), more than your %s max loss per "
                       "trade. No order. Skip this trade, or plan a closer stop with trader-exit."
                       % (money(loss_ps), _price(ref), _price(stop), money(max_loss)))
        else:
            refusal = ("one share of %s costs %s, more than your %s limit (%s). No order."
                       % (sym, money(ref), bound[0], limits[[l["name"] for l in limits].index(bound[0])]["how"]))
    elif qty == 0:
        refusal = ("%d share%s of %s is under the %d-share standard lot and \"fractional\" is off. No order. "
                   "Turn on the fractional market, or skip it." % (raw_qty, "" if raw_qty == 1 else "s", sym, lot))
    elif _f(c.get("min_reward_risk")) and reward_c < _f(c["min_reward_risk"]) * risk_c - EPS:
        refusal = ("reward:risk %.2f (target %s, entry %s, stop %s) is under your minimum %s, so trader-risk "
                   "would block it. No order. Skip it, or make a new plan with trader-exit."
                   % (reward_c / float(risk_c), _price(target), _price(ref), _price(stop), _num(c["min_reward_risk"])))
    elif cost < float(c.get("min_order_brl") or 0) - 0.005:
        refusal = ("%d share%s of %s is %s, under your %s minimum order. No order."
                   % (qty, "" if qty == 1 else "s", ticker, money(cost), money(c["min_order_brl"])))
    return {
        "symbol": sym, "ticker": ticker, "book": book, "side": "buy", "setup": plan.get("setup", ""),
        "date": plan.get("date"), "entry_time": plan.get("entry_time", ""), "entry_plan": round_cent(entry_plan),
        "entry": ref, "entry_from": ref_from, "stop": stop, "stop_limit": stop_limit_for(stop, c), "target": target,
        "risk_per_share": risk, "fees_per_share": round(fee_ps, 4), "reward_risk": round(reward_c / float(risk_c), 2),
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer, "cash_available": available,
        "max_loss_per_trade_brl": max_loss, "limits": limits, "shares_fit": raw_qty, "lot_size": lot,
        "qty": qty, "bound_by": bound[0], "cost": cost, "fees": fees, "max_loss": loss,
        "flat_by": plan.get("flat_by") or c.get("flat_by"),
        "order_type": "limit" if limit is not None else "market", "refusal": refusal,
    }


def bracket_order(s):
    """The one proposed order: the B3 ticket fields (prices as strings) plus why and plan."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    o = {"symbol": s["ticker"], "side": "buy", "qty": str(int(s["qty"])), "type": s["order_type"],
         "time_in_force": "day"}
    if s["order_type"] == "limit":
        o["limit_price"] = _price(s["entry"])
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "day trade")
    bound = {"max loss": "%s max loss per trade" % money(s["max_loss_per_trade_brl"] or 0),
             "position cap": "position cap", "max order": "max order", "cash": "cash after the buffer"}[s["bound_by"]]
    lot_words = ""
    if s["shares_fit"] != s["qty"]:
        lot_words = "; %d fit, rounded down to the %d-share lot" % (s["shares_fit"], s["lot_size"])
    why = ("%s on %s: %d shares (%s), bound by your %s%s; %d x %s + %s fees = %s at the stop"
           % (setup, s["symbol"], s["qty"], s["book"], bound, lot_words, s["qty"], _price(s["risk_per_share"]),
              money(s["fees"]), money(s["max_loss"])))
    o.update({
        "order_class": "bracket",
        "take_profit_limit_price": _price(s["target"]),
        "stop_loss_stop_price": _price(s["stop"]),
        "stop_loss_limit_price": _price(s["stop_limit"]),
        "why": why,
        "plan": {"entry": s["entry"], "stop": s["stop"], "target": s["target"],
                 "risk_per_share": s["risk_per_share"], "fees": s["fees"], "max_loss": s["max_loss"],
                 "setup": s["setup"], "entry_time": s["entry_time"], "flat_by": s["flat_by"]},
    })
    return o


def plan_report(s, order, notes=()):
    lines = []
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "setup")
    lines.append("%s buy, %s, entry %s%s" % (s["symbol"], setup, money(s["entry_plan"]),
                                            (" at " + s["entry_time"]) if s["entry_time"] else ""))
    lines.append("Stop %s%s. Target %s%s." % (money(s["stop"]), (": " + s["stop_label"]) if s["stop_label"] else "",
                                             money(s["target"]),
                                             (": " + s["target_label"]) if s["target_label"] else ""))
    for n in notes:
        lines.append(n)
    src = {"plan": "the plan's entry", "price": "the latest price, above the plan's entry",
           "limit": "your limit price"}[s["entry_from"]]
    lines.append("Sized at %s (%s). Risk %s a share (entry minus stop) plus about %s in fees. Reward:risk %.2f."
                 % (money(s["entry"]), src, money(s["risk_per_share"]), money(s["fees_per_share"]),
                    s["reward_risk"]))
    lines.append("")
    lines.append("Limits (whole shares; the smallest one wins):")
    for l in s["limits"]:
        lines.append("  %-13s %-62s %5d shares%s" % (l["name"], l["how"], l["shares"],
                                                      "  <- binds" if l["binds"] else ""))
    if s["shares_fit"] and s["qty"] and s["qty"] != s["shares_fit"]:
        lines.append("  lots          %d fit; B3's standard lot is %d, so %d (the rest would need the "
                     "fractional book)" % (s["shares_fit"], s["lot_size"], s["qty"]))
    lines.append("")
    if order is None:
        lines.append("No order: " + s["refusal"])
        lines.append("Education and tooling, not financial advice.")
        return "\n".join(lines)
    lines.append("Size: %d %s (%s) = %s at %s. Max loss at the stop: %d x %s + %s fees = %s."
                 % (s["qty"], s["ticker"], s["book"], money(s["cost"]), money(s["entry"]), s["qty"],
                    money(s["risk_per_share"]), money(s["fees"]), money(s["max_loss"])))
    lines.append("The stop is a stop-limit: trigger %s, limit %s. In a fast fall the price can gap past the limit "
                 "and it does not fill at all." % (money(s["stop"]), money(s["stop_limit"])))
    lines.append("")
    lines.append("Proposed order (nothing has been placed):")
    how = "limit %s" % order["limit_price"] if order["type"] == "limit" else "market"
    lines.append("  buy %s %s, %s, day; take-profit %s; stop-loss trigger %s, limit %s"
                 % (order["qty"], order["symbol"], how, order["take_profit_limit_price"],
                    order["stop_loss_stop_price"], order["stop_loss_limit_price"]))
    if s["flat_by"]:
        lines.append("Flat by %s Sao Paulo: anything still open then gets closed (trader-alerts rings, "
                     "trader-broker flatten)." % s["flat_by"])
    lines.append("")
    lines.append("Next: trader-risk checks it, then trader-broker asks you yes or no.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- orders file

def write_orders(path, orders, cfg, source="", now_utc=None):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    doc = {
        "created_by": "trader-budget",
        "created_at": b3.iso_utc(now_utc),
        "status": "proposed",
        "market": "B3",
        "currency": "BRL",
        "paper": bool(cfg.get("paper", True)),
        "note": ("Proposals only. Nothing is placed until trader-risk passes each order "
                 "and you say yes to it in the chat, then type it into your broker yourself."),
        "source": source,
        "orders": orders,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- main

def run_plan(a, cfg):
    now = parse_now(a.now)
    today = b3.to_brt(now).date().isoformat()
    plan = read_json(a.plan, "plan (run trader-exit first)")
    limit = None
    if a.limit is not None:
        if a.limit == "plan":
            limit = _f((plan or {}).get("entry"))
        else:
            limit = b3.parse_number(a.limit)
            if limit is None or limit <= 0:
                raise SizeError("--limit wants a price, like --limit 44.10")
    sym = str((plan or {}).get("symbol") or "").upper()
    notes, price = [], None
    ppath = a.prices or ("trader/prices.json" if os.path.exists("trader/prices.json") else None)
    if ppath and limit is None and sym:
        pdoc = read_json(ppath, "prices")
        p, t, fresh = fresh_price(pdoc, sym, now)
        if p is not None and fresh:
            price = p
            if round_cent(p) > round_cent(_f(plan.get("entry")) or 0):
                notes.append("%s last %s (quote of %s Sao Paulo) is above the plan's entry %s, so the risk "
                             "is bigger: sized at %s." % (sym, money(p), b3.to_brt(t).strftime("%H:%M"),
                                                          money(plan.get("entry")), money(p)))
        elif p is not None:
            notes.append("%s in %s is %s: not used (older than %d minutes). Sized at the plan's entry."
                         % (sym, ppath, ("from " + b3.to_brt(t).strftime("%Y-%m-%d %H:%M") + " Sao Paulo")
                            if t else "undated", FRESH_MINUTES))
    cash = None
    if a.portfolio and os.path.exists(a.portfolio):
        pf = read_json(a.portfolio, "portfolio")
        cash = _f(pf.get("cash")) if isinstance(pf, dict) else None
    s = size_trade(plan, cfg, a.budget, limit, price, cash, today)
    order = bracket_order(s)
    if order and a.write:
        src = "trader/plan.json (%s %s %s)" % (s["symbol"], s["setup"] or "plan", s["date"] or today)
        write_orders(a.write, [order], cfg, src, now)
    if a.json:
        print(json.dumps({"sizing": s, "orders": [order] if order else [], "notes": notes}, indent=2,
                         ensure_ascii=False))
    else:
        print(plan_report(s, order, notes))
        if order and a.write:
            print("Wrote 1 proposed order to %s" % a.write)
    return 0 if order else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Size a B3 day trade to your budget and max loss (trader-budget).")
    ap.add_argument("--plan", default="trader/plan.json", help="the trade plan (default trader/plan.json)")
    ap.add_argument("--config", default="trader/config.json", help="rules (default trader/config.json)")
    ap.add_argument("--budget", type=float, help="override the budget in the config (reais)")
    ap.add_argument("--limit", nargs="?", const="plan", metavar="PRICE",
                    help="a limit order instead of market, at the plan's entry or at PRICE")
    ap.add_argument("--prices", help="prices JSON (default trader/prices.json if it exists)")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; its cash caps the size if it exists")
    ap.add_argument("--now", help="pretend it is this Sao Paulo time, e.g. \"2026-11-12 10:25\"")
    ap.add_argument("--write", nargs="?", const="trader/orders.json", metavar="PATH",
                    help="write the proposed order (default trader/orders.json)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the report")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
        return run_plan(a, cfg)
    except SizeError as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
