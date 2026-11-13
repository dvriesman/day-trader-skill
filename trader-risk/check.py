#!/usr/bin/env python3
"""Pre-trade risk check for trader-risk. Standard library only, no network.

Reads the proposed orders (trader/orders.json) and checks every one against
your rules (trader/config.json), your account (trader/portfolio.json),
prices (trader/prices.json) and what was placed today (trader/state/broker.json).
Each order gets PASS or BLOCK with the reasons. Orders are checked in file
order as if each PASS had filled, so buys that are fine one by one but break
a limit together are caught.

    python3 check.py                                 # the trader/ files, writes trader/state/risk.json
    python3 check.py --orders trader/orders.json --json
    python3 check.py --no-write                      # check without recording the verdicts
    python3 check.py --now "2026-11-12 10:25"        # a REPLAY: pins the clock (Sao Paulo time)

A pinned clock (--now or --today) is for replays and tests. Its verdicts are
recorded as "clock_pinned": true, and trader-broker never shows an order as
READY on a pinned verdict unless trader-broker is pinned to the same replay
too. Only a check on the real clock can clear an order for a real yes.

Entries and exits. An ENTRY opens or adds to a position (a buy, or a sell
that would go short). An EXIT only reduces what you hold. Getting out must
always be possible: an exit is checked for format, asset and short sales
only; every other rule turns into a warning for it. PETR4 and PETR4F are the
same share: a fractional sell can close a standard-lot buy and vice versa.

What blocks an order (every order):

  format        missing symbol or side, a qty that is not a whole number above 0,
                a standard-lot ticker (PETR4) not in whole lots of lot_size (100),
                a fractional ticker (PETR4F) outside 1 to lot_size - 1, a limit
                order without a limit price, an unknown type or time in force;
                an order with a stop and a target that is not a day order or
                misses a leg, a stop-limit whose limit is above its trigger
  asset         an option (B3 option tickers, like PETRK440) unless
                "allow_options": true; anything that is not a B3 cash-market
                ticker (4 letters and 1 or 2 digits, with an optional F)
  short         selling more than you hold, unless "allow_short": true

What blocks an entry (on top):

  blocked       a ticker on your "blocked" list
  size          under min_order_brl, over max_order_brl
  position      the stock would be over max_position_pct of the account
  cash          the buy would dip into your cash buffer, or need margin
  duplicate     the same share and side twice in this batch, or already
                placed today (trader/state/broker.json)
  daily limit   max_orders_per_day orders placed today
  stop          no stop while "require_stop": true (a stop_loss_stop_price
                or the order's plan stop); a stop on the wrong side of the entry
  loss          qty x (entry - stop) + fees over max_loss_per_trade_brl. The
                entry is the limit price for a limit order, else the HIGHER of
                the plan's entry and a fresh price (quote under 20 minutes old:
                free B3 quotes run about 15 minutes late), so a stale plan
                cannot understate the risk. Fees: "fees_pct" per side.
  reward:risk   (target - entry) / (entry - stop) under min_reward_risk
  daily loss    the max losses at the stops of today's entries plus this one
                over max_daily_loss_brl (win or lose, each counts in full)
  trades        max_trades_per_day entries already placed today
  window        before no_entries_before or after no_entries_after (Sao Paulo
                time), or B3 is not in continuous trading (weekend, B3 holiday,
                before the open, the closing call, after the close; the session
                ends at 16:55 while the US is on daylight time, else 17:55)

Exit code 0 when every order passes, 1 when any order is blocked.
The verdicts go to trader/state/risk.json with a fingerprint of each order,
so trader-broker only shows an order as ready if THIS exact order passed today.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import b3  # noqa: E402

DEFAULTS = {
    "budget": None, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_brl": 15000,
    "min_order_brl": 50, "max_loss_per_trade_brl": 100, "max_daily_loss_brl": 300, "max_trades_per_day": 3,
    "max_orders_per_day": 10, "min_reward_risk": 1.0, "require_stop": True,
    "no_entries_before": "10:15", "no_entries_after": "11:30", "flat_by": "11:45",
    "lot_size": 100, "fractional": True, "fees_pct": 0.03,
    "paper": True, "blocked": [], "allow_options": False, "allow_margin": False, "allow_short": False,
}
ORDER_FIELDS = ("symbol", "side", "qty", "type", "time_in_force", "limit_price", "order_class",
                "take_profit_limit_price", "stop_loss_stop_price", "stop_loss_limit_price")
PRICE_FIELDS = ("limit_price", "take_profit_limit_price", "stop_loss_stop_price", "stop_loss_limit_price")
TYPES = {"market", "limit"}
TIFS = {"day", "ioc", "fok"}
OPTION = re.compile(r"^[A-Z][A-Z0-9]{3}[A-X][0-9]{1,4}[A-Z]?$")   # PETRK440, VALEW650E
CENT = 0.005
FRESH_MINUTES = 20
REF_WORDS = {"plan": "the plan's entry", "price": "the latest price", "limit": "the limit price",
             "stale price": "an old price"}


class RiskError(Exception):
    pass


# ---------------------------------------------------------------- time

to_brt = b3.to_brt


def parse_now(text):
    try:
        return b3.parse_now(text)
    except ValueError as e:
        raise RiskError("--now %s" % e)


def hhmm(value, key):
    """"10:15" -> time(10, 15); None or "" -> None (that bound is off)."""
    if value in (None, "", False):
        return None
    try:
        return b3.parse_hhmm(value)
    except ValueError:
        raise RiskError("%s must be a Sao Paulo time like \"10:15\", got %r" % (key, value))


def window_check(now_brt, c):
    """Reasons an entry cannot be placed now (Sao Paulo time). Empty when the window is off."""
    before = hhmm(c.get("no_entries_before"), "no_entries_before")
    after = hhmm(c.get("no_entries_after"), "no_entries_after")
    if before is None and after is None:
        return [], []
    warns = []
    d, t = now_brt.date(), now_brt.time().replace(second=0, microsecond=0)
    note = b3.calendar_note(d)
    if note:
        warns.append(note)
    ok, why = b3.trading_day(d)
    stamp = now_brt.strftime("%a %Y-%m-%d %H:%M")
    if not ok:
        return ["window: B3 is closed today (%s, %s Sao Paulo)" % (why, stamp)], warns
    o, close = b3.session(d, c)
    if now_brt.time() < o or now_brt.time() >= close:
        return ["window: B3 is not in continuous trading (%s Sao Paulo; today it runs %s to %s, then the "
                "closing call)" % (stamp, b3.hm(o), b3.hm(close))], warns
    sn = b3.session_note(d)
    if sn:
        warns.append(sn)
    if after is not None and after >= close:
        after = (datetime.datetime.combine(d, close) - datetime.timedelta(minutes=10)).time()
        warns.append("continuous trading ends at %s today: no new entries after %s" % (b3.hm(close), b3.hm(after)))
    if before is not None and t < before:
        return ["window: no new entries before %s Sao Paulo (it is %s)" % (b3.hm(before), b3.hm(t))], warns
    if after is not None and t > after:
        return ["window: no new entries after %s Sao Paulo (it is %s)" % (b3.hm(after), b3.hm(t))], warns
    return [], warns


# ---------------------------------------------------------------- files and helpers

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise RiskError("%s is not valid JSON: %s" % (path, e))


def fingerprint(order):
    """Same function in trader-broker/ticket.py: the exact order that was checked."""
    core = {k: order[k] for k in ORDER_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


money = b3.brl


def _cent(x):
    """A price to the cent, half up (44.105 -> 44.11)."""
    return int(float(x) * 100 + 0.5 + 1e-9) / 100.0


def _p(x):
    return "%.2f" % float(x)


def share_of(sym):
    """The share a ticker trades: PETR4F -> PETR4. Anything odd stays as it is."""
    try:
        return b3.base(sym)
    except ValueError:
        return str(sym or "").upper()


def account(cfg, portfolio, prices):
    """Starting account: cash and position values, keyed by the share (PETR4F counts as PETR4).
    No portfolio = a fresh account funded with the budget."""
    warnings = []
    if portfolio is None:
        budget = _f(cfg.get("budget"))
        if not budget:
            raise RiskError("no trader/portfolio.json and no budget in the config: "
                            "nothing to check the orders against")
        return {"cash": budget, "shares": {}, "values": {}}, warnings
    cash = _f(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash\" number")
    shares, values = {}, {}
    for p in portfolio.get("positions", []):
        t = share_of(p.get("ticker") or p.get("symbol"))
        if not t:
            continue
        n = _f(p.get("shares", p.get("qty")))
        if n is None:
            n = sum(_f(l.get("shares")) or 0 for l in p.get("lots", []))
        shares[t] = shares.get(t, 0) + n
        if prices.get(t):
            values[t] = shares[t] * prices[t]
        else:
            basis = sum(_f(l.get("cost")) or 0 for l in p.get("lots", []))
            values[t] = values.get(t, 0) + basis
            warnings.append("no price for %s: valued at its cost basis %s" % (t, money(basis)))
    return {"cash": cash, "shares": shares, "values": values}, warnings


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def rec_is_entry(rec):
    """trader-broker logs the kind trader-risk gave; older records: a buy is an entry."""
    k = rec.get("kind")
    if k:
        return k == "entry"
    return str(rec.get("side", "")).lower() == "buy"


def placed_today(state, today):
    """Orders recorded as placed today by trader-broker."""
    out = []
    for rec in (state or {}).get("orders", []):
        if rec.get("status") == "placed" and rec_day(rec) == today:
            out.append(rec)
    return out


# ---------------------------------------------------------------- the check

def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}.

    now          naive UTC (default: the real clock); its Sao Paulo date is "today"
    price_times  {SYM: naive UTC time of that price}; None = every price counts as fresh"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    prices = {share_of(k): float(v) for k, v in (prices or {}).items() if _f(v)}
    if price_times is not None:
        price_times = {share_of(k): v for k, v in price_times.items()}
    now_utc = now or b3.utc_now()
    now_brt = to_brt(now_utc)
    today = today or now_brt.date().isoformat()
    acct, top_warn = account(c, portfolio, prices)
    blocked = {share_of(b) for b in c.get("blocked") or []}
    lot = int(c.get("lot_size") or b3.LOT)
    fees_pct = float(c.get("fees_pct") or 0)
    done_today = placed_today(state, today)
    count_today = len(done_today)
    entries_today = [r for r in done_today if rec_is_entry(r)]
    loss_today = 0.0
    for r in entries_today:
        ml = _f(r.get("max_loss"))
        if ml is None:
            top_warn.append("an entry placed today (%s %s) has no max loss in the log; not counted in "
                            "the daily loss" % (r.get("side"), r.get("symbol")))
        else:
            loss_today += ml
    placed_keys = {(share_of(r.get("symbol")), r.get("side")) for r in done_today}
    win_reasons, win_warns = window_check(now_brt, c)
    seen = set()
    results = []
    batch_entries, batch_loss = 0, 0.0
    if not c.get("paper", True):
        top_warn.append("LIVE account (\"paper\": false): these orders use real money")
    top_warn.extend(win_warns)

    def fresh(sym):
        if sym not in prices:
            return None
        if price_times is None:
            return prices[sym]
        t = price_times.get(sym)
        if t is None:
            return None
        age = (now_utc - t).total_seconds()
        return prices[sym] if -60 <= age <= FRESH_MINUTES * 60 else None

    for i, o in enumerate(orders, 1):
        reasons, warns, info = [], [], []
        later = []                       # entry-only reasons, turned into warnings for an exit
        sym = str(o.get("symbol", "")).strip().upper()
        share = share_of(sym)
        side = str(o.get("side", "")).strip().lower()
        typ = str(o.get("type", "market")).strip().lower()
        tif = str(o.get("time_in_force", "day")).strip().lower()
        qty = o.get("qty")
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        oc = str(o.get("order_class") or "").strip().lower()
        legs = o.get("take_profit_limit_price") is not None or o.get("stop_loss_stop_price") is not None
        bracket = oc == "bracket" or (not oc and legs)
        value = None
        q = _f(qty)

        # format
        if not sym:
            reasons.append("format: no symbol")
        if side not in ("buy", "sell"):
            reasons.append("format: side must be buy or sell, got %r" % o.get("side"))
        if o.get("notional") is not None:
            reasons.append("format: B3 takes orders in shares, not in reais (no notional)")
        if q is None or q <= 0 or abs(q - round(q)) > 1e-9:
            reasons.append("format: qty must be a whole number of shares above 0, got %r" % qty)
            q = None
        for k in PRICE_FIELDS:
            if o.get(k) is not None:
                v = _f(o.get(k))
                if v is None or v <= 0:
                    reasons.append("format: %s must be a positive number, got %r" % (k, o.get(k)))
                elif abs(v * 100 - round(v * 100)) > 1e-6:
                    reasons.append("format: %s %r is not on B3's R$ 0,01 tick" % (k, o.get(k)))
        if typ not in TYPES:
            reasons.append("format: unknown order type %r (market or limit)" % typ)
        if tif not in TIFS:
            reasons.append("format: unknown time_in_force %r (day, ioc or fok)" % tif)
        if typ == "limit" and o.get("limit_price") is None:
            reasons.append("format: a limit order needs a limit_price")
        if bracket:
            if tif != "day":
                reasons.append("format: an order with a stop and a target is a day order, got %r" % tif)
            if o.get("take_profit_limit_price") is None or o.get("stop_loss_stop_price") is None:
                reasons.append("format: an order with a stop and a target needs both take_profit_limit_price and "
                               "stop_loss_stop_price")
            sl, st = _f(o.get("stop_loss_limit_price")), _f(o.get("stop_loss_stop_price"))
            if sl is not None and st is not None and side == "buy" and sl > st + CENT:
                reasons.append("format: the stop-limit's limit %s is above its trigger %s" % (_p(sl), _p(st)))
        elif oc:
            warns.append("advanced order (%s): check every leg before you say yes" % oc)

        # asset and lots
        if sym and OPTION.match(sym):
            if not c.get("allow_options"):
                reasons.append("asset: %s is an option; options are off (\"allow_options\": false)" % sym)
            else:
                warns.append("option order: options can lose 100%% of what you pay")
        elif sym and not b3.is_ticker(sym):
            reasons.append("asset: %s is not a B3 cash-market ticker (like PETR4, VALE3, BOVA11 or PETR4F); "
                           "this pack trades B3 stocks, units, ETFs and BDRs only" % sym)
        elif sym and q is not None:
            if sym.endswith("F"):
                if not c.get("fractional", True):
                    reasons.append("format: %s is the fractional market and \"fractional\" is off" % sym)
                elif q >= lot:
                    reasons.append("format: the fractional market takes 1 to %d shares; %d %s should be a "
                                   "standard-lot order on %s" % (lot - 1, q, sym, share))
            elif int(round(q)) % lot:
                reasons.append("format: %s trades in lots of %d; %d is not a whole number of lots (use %s for "
                               "1 to %d shares)" % (sym, lot, q, b3.fractional(sym), lot - 1))

        # the reference price: what one share costs to get in
        plan_entry = _f(plan.get("entry"))
        live = fresh(share)
        ref, ref_from = None, None
        if typ == "limit":
            ref, ref_from = _f(o.get("limit_price")), "limit"
        if ref is None:
            cands = [(p, w) for p, w in ((plan_entry, "plan"), (live, "price")) if p]
            if cands:
                ref, ref_from = max(cands, key=lambda x: x[0])
                ref = _cent(ref)
                if live and ref_from == "price" and plan_entry and live > plan_entry + CENT:
                    info.append("%s last %s is above the plan's entry %s: the risk is measured from %s"
                                % (share, _p(ref), _p(plan_entry), _p(ref)))
            elif share in prices:
                ref, ref_from = prices[share], "stale price"
        if plan_entry and live is None and share in prices and price_times is not None and typ == "market":
            warns.append("the %s price in trader/prices.json is older than %d minutes: not used"
                         % (share, FRESH_MINUTES))
        if q and ref:
            value = q * ref

        # entry or exit
        held = acct["shares"].get(share, 0.0)
        held_value = acct["values"].get(share, 0.0)
        exiting = False
        if side == "sell" and held > 1e-9 and q is not None:
            exiting = q <= held + 1e-9
        elif side == "buy" and held < -1e-9 and q is not None:
            exiting = q <= -held + 1e-9
        kind = "exit" if exiting else "entry"
        if kind == "entry" and value is None and q:
            later.append("price: no price for %s to size a %s-share order; add it to "
                         "trader/prices.json (trader-alerts: alerts.py quote %s --write)" % (share, qty, share))

        # blocked list and size
        if share in blocked:
            later.append("blocked: %s is on your blocked list" % share)
        if value is not None:
            if value < float(c.get("min_order_brl") or 0) - CENT:
                later.append("size: %s is under your %s minimum order"
                             % (money(value), money(c["min_order_brl"])))
            if c.get("max_order_brl") and value > float(c["max_order_brl"]) + CENT:
                later.append("size: %s is over your %s max order"
                             % (money(value), money(c["max_order_brl"])))

        # account effects, simulated in order
        cash = acct["cash"]
        total = cash + sum(acct["values"].values())
        buffer_brl = total * float(c.get("cash_buffer_pct") or 0) / 100.0
        if value is not None and side == "buy" and kind == "entry":
            after_cash = cash - value
            if after_cash < -CENT and not c.get("allow_margin"):
                later.append("cash: needs %s but only %s cash; that would be margin or day-trade leverage "
                             "(borrowed money)" % (money(value), money(cash)))
            elif after_cash < buffer_brl - CENT:
                later.append("cash: leaves %s cash, under your %s%% buffer of %s"
                             % (money(max(after_cash, 0)), _pct(c["cash_buffer_pct"]), money(buffer_brl)))
            pos_after = held_value + value
            limit = total * float(c.get("max_position_pct") or 100) / 100.0
            if total > 0 and pos_after > limit + CENT:
                later.append("position: %s would be %s, %.1f%% of the %s account, over your %s%% cap"
                             % (share, money(pos_after), 100.0 * pos_after / total, money(total),
                                _pct(c["max_position_pct"])))
        if side == "sell" and sym and kind == "entry":
            over = (q > held + 1e-9) if q is not None else held <= 0
            if over and not c.get("allow_short"):
                reasons.append("short: selling more %s than you hold (%s shares); short selling is off. "
                               "If you bought it, record the fill first (ticket.py fill)" % (share, _num(held)))

        # day-trade checks: stop, loss, reward:risk, daily loss, trades, window
        max_loss = risk_ps = rr = stop = target = None
        if kind == "entry" and side in ("buy", "sell"):
            long = side == "buy"
            leg_stop, plan_stop = _f(o.get("stop_loss_stop_price")), _f(plan.get("stop"))
            stop = leg_stop if leg_stop else plan_stop
            target = _f(o.get("take_profit_limit_price")) or _f(plan.get("target"))
            if leg_stop and plan_stop and abs(leg_stop - plan_stop) > CENT:
                warns.append("the order's stop %s differs from the plan's stop %s: the order's is the one "
                             "your broker uses" % (_p(leg_stop), _p(plan_stop)))
            if stop and not leg_stop:
                warns.append("the stop %s is in the plan only, not on the order: set a stop alert "
                             "(trader-alerts: alerts.py add --from-plan)" % _p(stop))
            if not stop:
                if c.get("require_stop", True):
                    later.append("stop: no stop. Every entry needs one (\"require_stop\": true): a "
                                 "stop_loss_stop_price, from trader-exit and trader-budget")
                else:
                    warns.append("no stop: the max loss is not capped, so it is not counted in the daily loss")
            elif ref:
                if (long and stop >= ref - CENT) or (not long and stop <= ref + CENT):
                    later.append("stop: the stop %s must be %s the entry %s for a %s"
                                 % (_p(stop), "below" if long else "above", _p(ref), side))
                else:
                    risk_c = int(round(abs(ref - stop) * 100))
                    risk_ps = risk_c / 100.0
                    shares = q or 0
                    fees = shares * (ref + stop) * fees_pct / 100.0
                    max_loss = round(shares * risk_ps + fees, 2)
                    cap = _f(c.get("max_loss_per_trade_brl"))
                    if cap and max_loss > cap + CENT:
                        later.append("loss: %s shares x %s risk a share + %s fees = %s at the stop, over your %s "
                                     "max loss per trade" % (_num(shares), _p(risk_ps), money(fees),
                                                             money(max_loss), money(cap)))
                    min_rr = _f(c.get("min_reward_risk"))
                    if target:
                        if (long and target <= ref + CENT) or (not long and target >= ref - CENT):
                            later.append("target: the target %s must be %s the entry %s"
                                         % (_p(target), "above" if long else "below", _p(ref)))
                        else:
                            rr = round(int(round(abs(target - ref) * 100)) / float(risk_c), 2)
                            if min_rr and rr < min_rr - 1e-9:
                                later.append("reward:risk: %.2f (target %s, entry %s, stop %s) is under your "
                                             "minimum %s" % (rr, _p(target), _p(ref), _p(stop), _num(min_rr)))
                    elif min_rr:
                        later.append("reward:risk: no target to measure it against (min_reward_risk %s)"
                                     % _num(min_rr))
            daily = _f(c.get("max_daily_loss_brl"))
            if daily and max_loss is not None:
                used = loss_today + batch_loss
                if used + max_loss > daily + CENT:
                    later.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                 "daily loss limit" % (money(used), money(max_loss), money(used + max_loss),
                                                       money(daily)))
            cap_t = int(c.get("max_trades_per_day") or 0)
            if cap_t and len(entries_today) + batch_entries >= cap_t:
                later.append("trades: %d entries placed today plus %d passed earlier in this batch, your limit "
                             "is %d a day" % (len(entries_today), batch_entries, cap_t))
            later.extend(win_reasons)
            if stop and ref and risk_ps:
                info.append("entry %s (%s), stop %s, target %s: risk %s a share"
                            % (_p(ref), REF_WORDS.get(ref_from, ref_from), _p(stop),
                               _p(target) if target else "none", _p(risk_ps)))
                info.append("max loss %s with fees%s, reward:risk %s%s"
                            % (money(max_loss), (" (limit %s)" % money(c["max_loss_per_trade_brl"]))
                               if _f(c.get("max_loss_per_trade_brl")) else "",
                               ("%.2f" % rr) if rr is not None else "n/a",
                               (" (min %s)" % _num(c["min_reward_risk"])) if _f(c.get("min_reward_risk")) else ""))
            if daily or cap_t:
                info.append("today before this: %d of %s trades, %s of %s daily loss"
                            % (len(entries_today) + batch_entries, cap_t or "unlimited",
                               money(loss_today + batch_loss), money(daily) if daily else "no limit on"))

        # duplicates and the daily order limit
        key = (share, side)
        if key in seen:
            later.append("duplicate: a second %s %s in this batch" % (side, share))
        if key in placed_keys:
            later.append("duplicate: a %s %s was already placed today" % (side, share))
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and count_today + passed_so_far >= cap_n:
            later.append("daily limit: %d placed today plus %d passed earlier in this batch, "
                         "your limit is %d a day" % (count_today, passed_so_far, cap_n))
        seen.add(key)

        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("exit, not blocked: " + x for x in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            if kind == "entry" and side in ("buy", "sell"):
                batch_entries += 1
                batch_loss += max_loss or 0.0
            if q is not None:
                if side == "buy":
                    if value is not None:
                        acct["cash"] -= value
                        acct["values"][share] = acct["values"].get(share, 0.0) + value
                    acct["shares"][share] = held + q
                elif side == "sell":
                    if value is not None:
                        acct["cash"] += value
                        acct["values"][share] = max(acct["values"].get(share, 0.0) - value, 0.0)
                    acct["shares"][share] = held - q
        results.append({
            "index": i, "symbol": sym, "share": share, "side": side, "kind": kind,
            "amount": "%s shares" % qty,
            "value_brl": round(value, 2) if value is not None else None,
            "entry_ref": round(ref, 2) if ref else None, "entry_ref_from": ref_from,
            "stop": stop, "target": target, "risk_per_share": risk_ps, "max_loss": max_loss,
            "reward_risk": rr, "fingerprint": fingerprint(o), "verdict": verdict,
            "reasons": reasons, "warnings": warns, "info": info,
        })

    return {
        "date": today,
        "now_sao_paulo": now_brt.strftime("%Y-%m-%d %H:%M"),
        "paper": bool(c.get("paper", True)),
        "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn,
        "cash_after": round(acct["cash"], 2),
        "today": {"orders_placed": count_today, "entries_placed": len(entries_today),
                  "loss_at_risk": round(loss_today, 2)},
    }


def _pct(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


def report(res):
    lines = []
    if res.get("clock_pinned"):
        lines.append("REPLAY: the clock is pinned to %s Sao Paulo. These verdicts are for practice: "
                     "trader-broker will not clear a real order on them." % res["now_sao_paulo"])
        lines.append("")
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        lines.append("%-5s #%d %s %s %s, %s" % (r["verdict"], r["index"], r["side"], r["symbol"], r["amount"],
                                                r["kind"]))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked at %s Sao Paulo. Cash after the passed orders: %s (%s account)."
                 % (res["pass"], res["block"], res["now_sao_paulo"], money(res["cash_after"]),
                    "paper" if res["paper"] else "LIVE"))
    if res["block"]:
        lines.append("Blocked orders do not go to the broker. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. trader-broker still asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def load_prices(pdoc):
    """prices.json -> ({SYM: price}, {SYM: price time as naive UTC or None}).

    The price time is the quote's own time (detail.time), else when it was fetched
    (detail.fetched), else the file's as_of."""
    if not isinstance(pdoc, dict):
        return {}, {}
    prices = pdoc.get("prices", pdoc)
    if not isinstance(prices, dict):
        return {}, {}
    out, times = {}, {}
    as_of = b3.parse_utc(pdoc.get("as_of"))
    detail = pdoc.get("detail") or {}
    for s, p in prices.items():
        if _f(p) is None:
            continue
        s = str(s).upper()
        out[s] = float(p)
        d = detail.get(s) or {}
        times[s] = b3.parse_utc(d.get("time")) or b3.parse_utc(d.get("fetched")) or as_of
    return out, times


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check proposed orders against your limits (trader-risk).")
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; if missing, a fresh account funded with the budget")
    ap.add_argument("--prices", default="trader/prices.json")
    ap.add_argument("--state", default="trader/state/broker.json", help="what trader-broker placed today")
    ap.add_argument("--now", help="REPLAY: pretend it is this Sao Paulo time, e.g. \"2026-11-12 10:25\"")
    ap.add_argument("--today", help="REPLAY: YYYY-MM-DD (default: today in Sao Paulo)")
    ap.add_argument("--out", default="trader/state/risk.json", help="where the verdicts are recorded")
    ap.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        now = parse_now(a.now)
        if a.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", a.today):
            raise RiskError("--today wants a date like 2026-11-12")
        doc = read_json(a.orders, "orders file (run trader-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        cfg = read_json(a.config, "config", required=False) or {}
        old = sorted(k for k in cfg if str(k).endswith("_usd"))
        if old:
            raise RiskError("%s has US-dollar keys (%s): this pack trades B3 in reais, rename them to _brl"
                            % (a.config, ", ".join(old)))
        portfolio = read_json(a.portfolio, "portfolio", required=False)
        pdoc = read_json(a.prices, "prices", required=False) or {}
        prices, times = load_prices(pdoc)
        state = read_json(a.state, "broker state", required=False)
        res = check(orders, cfg, portfolio, prices, state, a.today, now, times)
        res["clock_pinned"] = bool(a.now or a.today)
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            d = os.path.dirname(a.out)
            if d:
                os.makedirs(d, exist_ok=True)
            rec = dict(res)
            rec["checked_at"] = b3.iso_utc()
            rec["orders_file"] = a.orders
            tmp = a.out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, a.out)
    except RiskError as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2, ensure_ascii=False) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
