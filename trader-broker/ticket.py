#!/usr/bin/env python3
"""Order tickets and the decision log for trader-broker. Standard library only.

This tool never talks to a broker and never places anything, and nothing in
this pack can: on B3 YOU type the order into your own platform (the home
broker, Profit, MetaTrader 5, the broker's app). The tool does its work around
the one moment that matters, your yes:

  show      renders each proposed order as a plain ticket, written the way B3
            platforms ask for it: buy or sell, ticker, quantity, market or
            limit, and the OCO pair (stop-loss trigger and limit, take-profit).
            An order is READY only if trader-risk passed THIS exact order
            (matched by fingerprint) on the real clock in the last 15 minutes,
            and nobody has decided on it yet.
  approve   records your explicit yes for ONE order, with your reply word for
            word. "yes" or "sim" counts; it refuses "yes to all" / "sim, todas",
            "sure", "ok", "pode", a yes that names a different stock or amount,
            and any order that is not READY.
  decline   records a no.
  result    records what happened after you typed it in: placed (with the
            broker's order number), not_placed, or failed.
  fill      records a fill in trader/portfolio.json (cash and shares).
  flatten   builds the sell ticket(s) that close a position by flat_by. It
            still goes through trader-risk and your yes.
  log       prints today's decisions.

    python3 ticket.py show                    # every order, READY or not
    python3 ticket.py show --index 1 --json   # one ticket as JSON
    python3 ticket.py approve --index 1 --reply "sim"
    python3 ticket.py result --index 1 --status placed --broker-id 123456 --filled-qty 200 --filled-price 44.11
    python3 ticket.py fill --symbol PETR4 --side sell --qty 200 --price 44.54
    python3 ticket.py flatten --symbol PETR4 --qty 200
    python3 ticket.py log

Everything is written to trader/state/broker.json (append only). Dates are
Sao Paulo dates. --now "2026-11-12 10:26" pins the clock: a REPLAY, which
only accepts risk verdicts that were pinned too, and says so on every ticket.
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

ORDER_FIELDS = ("symbol", "side", "qty", "type", "time_in_force", "limit_price", "order_class",
                "take_profit_limit_price", "stop_loss_stop_price", "stop_loss_limit_price")
TIF_WORDS = {"day": "good for today (dia)", "ioc": "immediate or cancel (FAK)", "fok": "fill or kill (FOK)"}
YES_WORDS = ("yes", "y", "sim", "s")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each",
               "todas", "todos", "tudo", "ambas", "ambos", "cada", "restante", "restantes"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later", "hold",
               "unless", "if", "nao", "não", "espera", "espere", "cancela", "cancele", "mas", "talvez",
               "depois", "se", "porem", "porém", "exceto"}
RISK_MAX_AGE_MIN = 15


class TicketError(Exception):
    pass


# ---------------------------------------------------------------- time

def parse_now(text):
    try:
        return b3.parse_now(text)
    except ValueError as e:
        raise TicketError("--now %s" % e)


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise TicketError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def rec_day(rec):
    """The trading day a decision belongs to: its "date", else the day of "at"."""
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


# ---------------------------------------------------------------- the order

def fingerprint(order):
    """Same function as trader-risk/check.py: identifies the exact order."""
    core = {k: order[k] for k in ORDER_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _str_num(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        f = float(v)
        return str(int(f)) if f == int(f) else repr(f)
    return str(v)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def share_of(sym):
    try:
        return b3.base(sym)
    except ValueError:
        return str(sym or "").upper()


def order_fields(order):
    """The order as the fields it is made of (the fingerprinted part). "why" and "plan" are ours."""
    out = {}
    for k in ORDER_FIELDS:
        if k in order and order[k] is not None:
            out[k] = _str_num(order[k]) if k not in ("symbol", "side", "type", "time_in_force", "order_class") \
                else order[k]
    return out


money = b3.brl
_p = b3.px


def is_bracket(order):
    oc = str(order.get("order_class") or "").lower()
    legs = order.get("take_profit_limit_price") is not None or order.get("stop_loss_stop_price") is not None
    return oc == "bracket" or (not oc and legs)


def max_loss(order):
    """(reais, stop) lost if the stop fills exactly at its trigger, or (None, stop)."""
    stop = _f(order.get("stop_loss_stop_price")) or _f((order.get("plan") or {}).get("stop"))
    q = _f(order.get("qty"))
    if not stop or not q:
        return None, stop
    plan = order.get("plan") or {}
    if _f(plan.get("max_loss")) is not None and str(order.get("type", "market")) != "limit":
        return round(_f(plan["max_loss"]), 2), stop
    if str(order.get("type", "market")) == "limit" and _f(order.get("limit_price")):
        entry = _f(order["limit_price"])
    else:
        entry = _f(plan.get("entry"))
    if not entry:
        return None, stop
    risk = abs(entry - stop)
    return round(q * int(round(risk * 100)) / 100.0, 2), stop


def plain_parts(order):
    """The ticket in plain words, one part per line."""
    side = str(order.get("side", "")).lower()
    word = {"buy": "BUY (compra)", "sell": "SELL (venda)"}.get(side, side.upper())
    sym = order.get("symbol", "?")
    typ = str(order.get("type", "market"))
    tif = TIF_WORDS.get(str(order.get("time_in_force", "day")), order.get("time_in_force"))
    q = _str_num(order.get("qty"))
    how = ("limit %s" % _p(order["limit_price"])) if typ == "limit" and order.get("limit_price") else "market (a mercado)"
    book = " (fractional market)" if str(sym).endswith("F") else ""
    parts = ["%s %s %s%s, %s, %s" % (word, q, sym, book, how, tif)]
    if is_bracket(order):
        if order.get("stop_loss_stop_price") is not None:
            leg = "stop-loss: trigger (disparo) %s" % _p(order["stop_loss_stop_price"])
            if order.get("stop_loss_limit_price") is not None:
                leg += ", limit (limite) %s" % _p(order["stop_loss_limit_price"])
            parts.append(leg)
        if order.get("take_profit_limit_price") is not None:
            parts.append("take-profit (alvo): limit %s" % _p(order["take_profit_limit_price"]))
        loss, stop = max_loss(order)
        if loss is not None:
            parts.append("max loss about %s if the stop fills at %s (it can fill worse, or not at all past its "
                         "limit, in a fast market)" % (money(loss), _p(stop)))
    return parts


def plain(order):
    return " / ".join(plain_parts(order))


def amount_numbers(order):
    nums = []
    for k in ("qty", "limit_price", "stop_loss_stop_price", "take_profit_limit_price", "stop_loss_limit_price"):
        if order.get(k) is not None:
            v = _f(order[k])
            if v is not None:
                nums.append(v)
    entry = _f((order.get("plan") or {}).get("entry"))
    if entry is not None:
        nums.append(entry)
    loss, _ = max_loss(order)
    if loss is not None:
        nums.append(loss)
    return nums


def _reply_numbers(raw):
    """Numbers in a reply, read both ways where it matters: "44,10" and "44.10" are 44.1;
    "1.000" can be 1000 (Brazil) or 1.0."""
    out = []
    for tok in re.findall(r"\d[\d.,]*", raw):
        tok = tok.rstrip(".,")
        cands = set()
        v = b3.parse_number(tok)
        if v is not None:
            cands.add(v)
        if re.match(r"^\d{1,3}(\.\d{3})+$", tok):
            cands.add(float(tok.replace(".", "")))
        if re.match(r"^\d{1,3}(,\d{3})+$", tok):
            cands.add(float(tok.replace(",", "")))
        out.append(sorted(cands))
    return out


def parse_yes(reply, order, batch_symbols=()):
    """Is this reply an explicit yes for THIS one order? Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-zà-ú']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" or \"sim\" counts (not ok, sure, pode, manda)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = str(order.get("symbol", "")).upper()
    tokens = set(re.findall(r"[A-Z][A-Z0-9]{3}[0-9]{1,2}F?", raw.upper()))
    mine = {sym, share_of(sym), b3.fractional(share_of(sym)) if b3.is_ticker(sym) else sym}
    others = ({str(s).upper() for s in batch_symbols} | tokens) - mine
    named = tokens & others
    if named:
        return False, "the reply names %s, but this order is for %s" % (", ".join(sorted(named)), sym)
    nums = _reply_numbers(re.sub(r"[A-Z][A-Z0-9]{3}[0-9]{1,2}F?", " ", raw.upper()))
    if nums:
        want = amount_numbers(order)
        bad = [n for n in nums if not any(abs(c - w) < 0.005 for c in n for w in want)]
        if bad:
            return False, "the reply says %s, but this order is %s" % (
                ", ".join(_str_num(n[0]) for n in bad), plain(order))
    return True, "explicit yes for %s" % plain(order)


# ---------------------------------------------------------------- state

def load_state(path):
    return read_json(path, "broker state", required=False) or {"orders": []}


def latest(state, fp, today):
    """The latest decision recorded today for this fingerprint, or None."""
    found = None
    for rec in state.get("orders", []):
        if rec.get("fingerprint") == fp and rec_day(rec) == today:
            found = rec
    return found


def risk_verdict(risk, fp, today, now_utc=None, replay=False):
    """(result, None) for a usable verdict, else (None, why)."""
    if not risk:
        return None, "trader-risk has not checked these orders (run check.py)"
    if risk.get("clock_pinned") and not replay:
        return None, ("the last risk check was a replay (check.py --now): run check.py on the real clock")
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today; run check.py again" % risk.get("date")
    if not replay and now_utc is not None:
        at = b3.parse_utc(risk.get("checked_at"))
        if at is None:
            return None, "the risk check has no time on it; run check.py again"
        age = (now_utc - at).total_seconds() / 60.0
        if age > RISK_MAX_AGE_MIN or age < -1:
            return None, ("the risk check is %d minutes old (the limit is %d: the window, the price and today's "
                          "trades move); run check.py again" % (int(age), RISK_MAX_AGE_MIN))
    for r in risk.get("results", []):
        if r.get("fingerprint") == fp:
            return r, None
    return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"


def status_of(order, risk, state, today, now_utc=None, replay=False):
    """(ready, label, risk_result)"""
    fp = fingerprint(order)
    rv, why = risk_verdict(risk, fp, today, now_utc, replay)
    if rv is None:
        return False, "NOT READY: " + why, None
    if rv.get("verdict") != "PASS":
        return False, "BLOCKED by trader-risk: " + "; ".join(rv.get("reasons", [])), rv
    rec = latest(state, fp, today)
    if rec:
        st = rec.get("status")
        if st == "approved":
            return False, "APPROVED by you at %s, waiting for you to type it into your broker" % rec.get("at"), rv
        return False, "DONE: %s at %s" % (st, rec.get("at")), rv
    return True, "READY: trader-risk PASS, waiting for your yes", rv


def kind_of(order, rv):
    """entry or exit, as trader-risk saw it (older checks: a buy is an entry)."""
    if rv and rv.get("kind"):
        return rv["kind"]
    return "entry" if str(order.get("side", "")).lower() == "buy" else "exit"


# ---------------------------------------------------------------- rendering

def how_to_type(order):
    """Where each number goes in a B3 platform."""
    lines = ["  Type it into your broker's platform yourself:"]
    lines.append("    1. %s %s %s, %s." % ("Buy" if order.get("side") == "buy" else "Sell",
                                          _str_num(order.get("qty")), order.get("symbol"),
                                          ("limit %s" % _p(order["limit_price"])) if order.get("type") == "limit"
                                          else "at market"))
    if is_bracket(order):
        lines.append("    2. After it fills, an OCO order (stop duplo / ordem OCO) for the same %s shares:" %
                     _str_num(order.get("qty")))
        lines.append("       stop: disparo %s, limite %s; alvo (take-profit): %s." % (
            _p(order.get("stop_loss_stop_price")),
            _p(order.get("stop_loss_limit_price") or order.get("stop_loss_stop_price")),
            _p(order.get("take_profit_limit_price"))))
        lines.append("       No OCO in your platform? Place the stop-limit, and set the target as an alert.")
    return lines


def render(order, idx, total, cfg, risk, state, today, now_utc=None, replay=False):
    ready, label, rv = status_of(order, risk, state, today, now_utc, replay)
    paper = cfg.get("paper", True)
    acct = "PAPER / SIMULATOR" if paper else "LIVE ACCOUNT, REAL MONEY"
    lines = ["ORDER %d of %d, %s, B3%s" % (idx, total, acct, ", REPLAY (pinned clock)" if replay else "")]
    for part in plain_parts(order):
        lines.append("  " + part)
    if order.get("why"):
        lines.append("  why: " + order["why"])
    lines.append("  " + label)
    for w in (rv or {}).get("warnings", []):
        lines.append("  note: " + w)
    if ready:
        lines.extend(how_to_type(order))
        lines.append("  Reply \"sim\" (or \"yes\") for this one order, or \"no\" to skip it.")
    return "\n".join(lines), ready


def ticket_json(order, idx, cfg, risk, state, today, now_utc=None, replay=False):
    ready, label, rv = status_of(order, risk, state, today, now_utc, replay)
    loss, stop = max_loss(order)
    return {"index": idx, "ready": ready, "status": label, "plain": plain(order),
            "fingerprint": fingerprint(order), "paper": bool(cfg.get("paper", True)), "replay": replay,
            "market": "B3", "execution": "manual", "order": order_fields(order), "why": order.get("why", ""),
            "bracket": is_bracket(order), "max_loss": loss, "stop": stop}


# ---------------------------------------------------------------- fills -> portfolio

def apply_fill(portfolio_path, cfg, symbol, side, qty, price, date):
    """Record a filled order in trader/portfolio.json: cash and shares, with a
    cost-basis lot per buy (oldest lots are relieved first on a sell). PETR4F
    and PETR4 are one position (the same share)."""
    pf = read_json(portfolio_path, "portfolio", required=False)
    if pf is None:
        budget = cfg.get("budget")
        if not budget:
            raise TicketError("no %s yet and no budget in the config to start it from" % portfolio_path)
        pf = {"cash": float(budget), "currency": "BRL", "positions": []}
    try:
        symbol = b3.base(symbol)
    except ValueError as e:
        raise TicketError(str(e))
    qty, price = b3.parse_number(qty), b3.parse_number(price)
    if not qty or not price or qty <= 0 or price <= 0 or qty != int(qty):
        raise TicketError("a fill needs a whole positive qty and a positive price")
    cost = round(qty * price, 2)
    pos = None
    for p in pf.setdefault("positions", []):
        if share_of(p.get("ticker")) == symbol:
            pos = p
    if side == "buy":
        if pos is None:
            pos = {"ticker": symbol, "shares": 0.0, "lots": []}
            pf["positions"].append(pos)
        lots = pos.setdefault("lots", [])
        lots.append({"id": "%s-%d" % (symbol, len(lots) + 1), "date": date, "shares": qty, "cost": cost})
        pos["shares"] = round(float(pos.get("shares", 0)) + qty, 6)
        pf["cash"] = round(float(pf.get("cash", 0)) - cost, 2)
    else:
        if pos is None or float(pos.get("shares", 0)) + 1e-9 < qty:
            raise TicketError("cannot record selling %s %s: the portfolio holds %s"
                              % (_str_num(qty), symbol, pos.get("shares", 0) if pos else 0))
        left = qty
        for lot in sorted(pos.get("lots", []), key=lambda l: l.get("date", "")):
            if left <= 1e-9:
                break
            take = min(left, float(lot["shares"]))
            per = float(lot["cost"]) / float(lot["shares"]) if float(lot["shares"]) else 0
            lot["cost"] = round(float(lot["cost"]) - per * take, 2)
            lot["shares"] = round(float(lot["shares"]) - take, 6)
            left -= take
        pos["lots"] = [l for l in pos.get("lots", []) if float(l["shares"]) > 1e-9]
        pos["shares"] = round(float(pos["shares"]) - qty, 6)
        if pos["shares"] <= 1e-9:
            pf["positions"].remove(pos)
        pf["cash"] = round(float(pf.get("cash", 0)) + cost, 2)
    write_json(portfolio_path, pf)
    return pf


def log_fill(state_path, now, today, symbol, side, qty, price):
    """A fill in the log, so trader-risk can count today's trades."""
    state = load_state(state_path)
    q, p = b3.parse_number(qty), b3.parse_number(price)
    state.setdefault("orders", []).append({
        "at": b3.iso_brt(now), "date": today, "status": "filled", "symbol": str(symbol).upper(), "side": side,
        "qty": _str_num(q), "price": p,
        "plain": "%s %s %s at %s" % (side.upper(), _str_num(q), str(symbol).upper(), _p(p))})
    write_json(state_path, state)


# ---------------------------------------------------------------- commands

def _clock(a):
    replay = bool(getattr(a, "now", None) or getattr(a, "today", None))
    now = parse_now(getattr(a, "now", None))
    today = getattr(a, "today", None) or b3.to_brt(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
        raise TicketError("--today wants a date like 2026-11-12")
    return now, today, replay


def _load(a):
    doc = read_json(a.orders, "orders file (run trader-budget, or ticket.py flatten, first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % a.orders)
    cfg = read_json(a.config, "config", required=False) or {}
    risk = read_json(a.risk, "risk check", required=False)
    state = load_state(a.state)
    now, today, replay = _clock(a)
    return orders, cfg, risk, state, today, now, replay


def _pick(orders, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def cmd_show(a):
    orders, cfg, risk, state, today, now, replay = _load(a)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    if a.json:
        out = [ticket_json(o, i, cfg, risk, state, today, now, replay) for i, o in picks]
        print(json.dumps(out[0] if a.index else out, indent=2, ensure_ascii=False))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, risk, state, today, now, replay)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne order at a time: a yes covers one ticket, never the batch. Nothing here sends an order: you type "
          "it into your broker. Education and tooling, not financial advice.")
    return 0 if any_ready else 1


def cmd_approve(a):
    orders, cfg, risk, state, today, now, replay = _load(a)
    order = _pick(orders, a.index)
    ready, label, rv = status_of(order, risk, state, today, now, replay)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    ok, why = parse_yes(a.reply, order, [o.get("symbol", "") for o in orders])
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
        return 1
    loss, stop = max_loss(order)
    rec = {"at": b3.iso_brt(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": str(order.get("symbol", "")).upper(), "side": order.get("side"),
           "qty": order.get("qty"), "kind": kind_of(order, rv), "max_loss": loss, "stop": stop,
           "plain": plain(order), "status": "approved", "reply": a.reply, "replay": replay,
           "paper": bool(cfg.get("paper", True)), "execution": "manual"}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded your yes for order %d: %s." % (a.index, plain(order)))
    for line in how_to_type(order):
        print(line.strip() if line.startswith("  Type") else line)
    print("Then: ticket.py result --index %d --status placed|not_placed|failed (with --broker-id, and "
          "--filled-qty / --filled-price once it fills)." % a.index)
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, today, now, replay = _load(a)
    order = _pick(orders, a.index)
    rec = {"at": b3.iso_brt(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": str(order.get("symbol", "")).upper(), "side": order.get("side"),
           "plain": plain(order), "status": "declined", "reply": a.reply or "no"}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded: you said no to order %d (%s). Nothing to type in." % (a.index, plain(order)))
    return 0


def cmd_result(a):
    orders, cfg, risk, state, today, now, replay = _load(a)
    order = _pick(orders, a.index)
    fp = fingerprint(order)
    rec = latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        raise TicketError("order %d has no yes recorded today; a result can only follow an approval" % a.index)
    new = {"at": b3.iso_brt(now), "date": today, "index": a.index, "fingerprint": fp, "symbol": rec["symbol"],
           "side": rec["side"], "plain": rec["plain"], "status": a.status}
    for k in ("qty", "kind", "max_loss", "stop"):
        if rec.get(k) is not None:
            new[k] = rec[k]
    if a.broker_id:
        new["broker_order_id"] = a.broker_id
    if a.note:
        new["note"] = a.note
    state["orders"].append(new)
    write_json(a.state, state)
    print("Recorded order %d as %s." % (a.index, a.status))
    if a.status == "placed" and a.filled_qty and a.filled_price:
        apply_fill(a.portfolio, cfg, rec["symbol"], rec["side"], a.filled_qty, a.filled_price, today)
        log_fill(a.state, now, today, rec["symbol"], rec["side"], a.filled_qty, a.filled_price)
        print("Added the fill to %s." % a.portfolio)
    elif a.status == "placed":
        print("When it fills, record it: ticket.py fill --symbol %s --side %s --qty Q --price P"
              % (rec["symbol"], rec["side"]))
        if rec.get("kind") == "entry" and is_bracket(order):
            print("Then place the OCO stop / target for those shares, and record that sell the same way when "
                  "one side fills.")
    return 0


def cmd_fill(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now = parse_now(a.now)
    date = a.date or b3.to_brt(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise TicketError("--date wants a date like 2026-11-12")
    pf = apply_fill(a.portfolio, cfg, a.symbol, a.side, a.qty, a.price, date)
    log_fill(a.state, now, date, a.symbol, a.side, a.qty, a.price)
    print("Recorded %s %s %s at %s on %s in %s. Cash now %s." % (a.side, a.qty, a.symbol.upper(),
                                                                money(b3.parse_number(a.price)), date,
                                                                a.portfolio, money(pf["cash"])))
    return 0


def cmd_flatten(a):
    cfg = read_json(a.config, "config", required=False) or {}
    try:
        sym = b3.base(a.symbol)
    except ValueError as e:
        raise TicketError(str(e))
    now, today, replay = _clock(a)
    source = "your broker's position screen"
    if a.qty is not None:
        qty = b3.parse_number(a.qty)
        if qty is None or qty <= 0 or qty != int(qty):
            raise TicketError("--qty wants the whole number of shares you hold, like --qty 200")
    else:
        pf = read_json(a.portfolio, "portfolio", required=False) or {}
        qty = sum(_f(p.get("shares", p.get("qty"))) or 0 for p in pf.get("positions", [])
                  if share_of(p.get("ticker") or p.get("symbol")) == sym)
        source = a.portfolio
        if qty <= 1e-9:
            raise TicketError("nothing to flatten: no %s shares in %s. Check your broker's position screen and "
                              "pass --qty if you do hold some" % (sym, a.portfolio))
    lot = int(cfg.get("lot_size") or b3.LOT)
    lots, odd = b3.split_lots(int(qty), lot)
    plan = read_json(a.plan, "plan", required=False) or {}
    flat_by = (plan.get("flat_by") if share_of(plan.get("symbol")) == sym else None) \
        or cfg.get("flat_by") or "11:45"
    why = "flat by %s Sao Paulo: closes the %s day trade (qty from %s)" % (flat_by, sym, source)
    orders = []
    if lots:
        orders.append({"symbol": sym, "side": "sell", "qty": str(lots), "type": "market", "time_in_force": "day",
                       "why": why})
    if odd:
        orders.append({"symbol": b3.fractional(sym), "side": "sell", "qty": str(odd), "type": "market",
                       "time_in_force": "day", "why": why + "; the odd %d shares go to the fractional market" % odd})
    old = read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "trader-broker", "created_at": b3.iso_utc(now), "status": "proposed", "market": "B3",
           "paper": bool(cfg.get("paper", True)),
           "note": "Flatten ticket. Nothing is placed until trader-risk passes it, you say yes in the chat, and you "
                   "type it into your broker.",
           "source": "ticket.py flatten %s on %s" % (sym, today), "orders": orders}
    write_json(a.orders, doc)
    print("Flatten %s: %s (flat by %s Sao Paulo)." % (sym, "; ".join(plain(o) for o in orders), flat_by))
    print("Wrote it to %s%s." % (a.orders, (" (it replaced %d earlier proposal%s)"
                                             % (replaced, "" if replaced == 1 else "s")) if replaced else ""))
    if a.qty is None:
        print("The qty comes from %s. Check it against your broker's position screen before you say yes." % source)
    print("Next:")
    print("  1. trader-risk: check.py. A sell that only closes what you hold is never blocked by the daily")
    print("     limits or the entry window.")
    print("  2. ticket.py show, then the user's yes for each order, one at a time.")
    print("  3. Cancel the open OCO stop / target for these shares FIRST in your platform, or the broker may")
    print("     reject the sell, or sell the shares twice when the stop also fires.")
    return 0


def cmd_log(a):
    state = load_state(a.state)
    now, today, replay = _clock(a)
    rows = [r for r in state.get("orders", []) if a.all or rec_day(r) == today]
    if not rows:
        print("No decisions recorded%s." % ("" if a.all else " today"))
        return 1
    for r in rows:
        extra = (" id " + r["broker_order_id"]) if r.get("broker_order_id") else ""
        reply = (' reply "%s"' % r["reply"]) if r.get("reply") and r["status"] in ("approved", "declined") else ""
        loss = (" max loss %s" % money(r["max_loss"])) if r.get("max_loss") is not None and r["status"] == "placed" else ""
        idx = ("#%s" % r["index"]) if r.get("index") is not None else "  "
        print("%s  %s %-10s %s%s%s%s" % (r.get("at"), idx, r.get("status"), r.get("plain"), reply, loss, extra))
    placed = sum(1 for r in rows if r.get("status") == "placed")
    print("%d placed%s." % (placed, "" if a.all else " today"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Order tickets and your yes (trader-broker). Never places orders.")
    sub = ap.add_subparsers(dest="cmd")

    def clock(p):
        p.add_argument("--now", help="REPLAY: pretend it is this Sao Paulo time, e.g. \"2026-11-12 10:26\"")
        p.add_argument("--today", help="REPLAY: YYYY-MM-DD (default: today in Sao Paulo)")

    def common(p):
        p.add_argument("--orders", default="trader/orders.json")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--risk", default="trader/state/risk.json")
        p.add_argument("--state", default="trader/state/broker.json")
        clock(p)

    p = sub.add_parser("show", help="render the tickets")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve", help="record the user's explicit yes for one order")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="the user's reply, word for word")
    p = sub.add_parser("decline", help="record a no")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("result", help="record what happened after you typed it in")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--status", required=True, choices=["placed", "not_placed", "failed"])
    p.add_argument("--broker-id")
    p.add_argument("--note")
    p.add_argument("--filled-qty")
    p.add_argument("--filled-price")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p = sub.add_parser("fill", help="record a fill in trader/portfolio.json")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--qty", required=True)
    p.add_argument("--price", required=True)
    p.add_argument("--date", help="YYYY-MM-DD (default: today in Sao Paulo)")
    p.add_argument("--now")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default="trader/state/broker.json")
    p = sub.add_parser("flatten", help="sell ticket(s) that close a position by flat_by")
    p.add_argument("--symbol", required=True)
    p.add_argument("--qty", help="shares you hold, from your broker (default: trader/portfolio.json)")
    p.add_argument("--orders", default="trader/orders.json", help="where the ticket is written")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json")
    clock(p)
    p = sub.add_parser("log", help="today's decisions")
    p.add_argument("--state", default="trader/state/broker.json")
    clock(p)
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "approve": cmd_approve, "decline": cmd_decline, "result": cmd_result,
                "fill": cmd_fill, "flatten": cmd_flatten, "log": cmd_log}[a.cmd](a)
    except TicketError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
