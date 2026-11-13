#!/usr/bin/env python3
"""Price alerts and the B3 clock for trader-alerts. Standard library only.

    python3 alerts.py clock                          # is B3 open? today's session, next open
    python3 alerts.py quote PETR4 --write            # quotes -> trader/prices.json
    python3 alerts.py quote --holdings --write       # every ticker in your portfolio and plan
    python3 alerts.py add --from-plan                # stop, target and flat-by from trader/plan.json
    python3 alerts.py add --symbol PETR4 --stop 44.62
    python3 alerts.py add --symbol PETR4 --target 45.40
    python3 alerts.py add --at 11:45 --label "flat by" --before-close
    python3 alerts.py add --symbol VALE3 --pct 3
    python3 alerts.py add --opening-bell
    python3 alerts.py check                          # rings what hit, once; exit 1 if nothing rang
    python3 alerts.py check --now "2026-11-10 11:45" --offline tests/fixtures/trader-alerts
    python3 alerts.py list
    python3 alerts.py reset petr4-plan-stop
    python3 alerts.py set-price PETR4 44.12 --source xp-browser   # a live price you read in your broker

Rule types:
  above / target  the price is at or above "price"
  below / stop    the price is at or below "price"
  pct_move        the price moved "pct" percent or more from the previous close
                  (or from "ref" if you give one); "direction": "up" or "down"
  opening_bell    B3 opened today (rings once per trading day)
  time            the Sao Paulo clock passed "at" (HH:MM) on a trading day
                  (rings once per trading day); "before_close": true rings 10
                  minutes before the end of continuous trading if "at" is later
                  than that (B3 closes at 16:55 while the US is on daylight time)

A rule with "date" is only watched on that Sao Paulo date (rules from a plan
are for the plan's day only). An above / below alert rings ONCE and then stays
quiet until you reset it, unless the rule has "rearm": true (it re-arms when
the price crosses back). A pct_move alert rings at most once per day.

Quotes (free, DELAYED about 15 minutes for B3, unofficial; for alerts only,
never for timing an order):
  https://query2.finance.yahoo.com/v8/finance/chart/PETR4.SA (falls back to
  query1 on a 429 or a 5xx; one retry with a pause on a 429; one request per
  ticker, half a second apart). A fractional ticker (PETR4F) is quoted as its
  standard-lot share (PETR4): same company, same price.
  --offline DIR reads DIR/yahoo-<TICKER>.json (or .json.gz) instead.

Live prices from your broker (set-price). Free Yahoo quotes are late; your
broker's screen is not. When the user agrees to it, Claude reads the live
price off the user's own broker page (XP in the browser, read-only, the user
logged in themselves) and records it here with its source and the time it was
read. trader-budget and trader-risk then count it as fresh for 20 minutes.
A price typed in this way is only as good as the reading: the tool refuses a
price more than 20 % away from the last known quote, unless --force.

The B3 calendar and session grid live in b3.py (next to this file).
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import b3  # noqa: E402

UA = "Mozilla/5.0 (compatible; b3-day-trader-skill/2.0)"
PAUSE = 0.5    # seconds between requests: be polite to free endpoints
BACKOFF = 2.0  # seconds to wait before the one retry after a 429
YAHOO_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
MAX_BYTES = 5 * 1024 * 1024
BEFORE_CLOSE_MIN = 10


class AlertError(Exception):
    pass


def parse_now(text):
    try:
        return b3.parse_now(text)
    except ValueError as e:
        raise AlertError("--now %s" % e)


def _sym(s):
    try:
        return b3.norm(s)
    except ValueError as e:
        raise AlertError(str(e))


# ---------------------------------------------------------------- the clock

def market_status(now_utc, cfg=None):
    local = b3.to_brt(now_utc)
    d = local.date()
    ok, reason = b3.trading_day(d)
    st = {"now_sao_paulo": local.strftime("%Y-%m-%d %H:%M"), "date": d.isoformat(),
          "weekday": local.strftime("%A"), "calendar_note": b3.calendar_note(d),
          "open": False, "reason": reason, "session_note": b3.session_note(d)}
    s = b3.session(d, cfg)
    if s:
        o, c = s
        st["opens"], st["closes"], st["call_ends"] = b3.hm(o), b3.hm(c), b3.hm(b3.call_end(c))
        t = local.time()
        if o <= t < c:
            st["open"] = True
            st["reason"] = "continuous trading"
        elif t < o:
            st["reason"] = "before the open (pre-opening auction from 09:45)" if o == b3.SESSION["open"] \
                else "before the open"
        elif t < b3.call_end(c):
            st["reason"] = "closing call (auction), no continuous trading"
        else:
            st["reason"] = "after the close"
    nd = d if (s and local.time() < s[0]) else d
    st["next_open"] = ("%s %s" % (d.isoformat(), b3.hm(s[0]))) if (s and local.time() < s[0]) else b3.next_open(nd, cfg)
    return st


# ---------------------------------------------------------------- quotes

def yahoo_url(sym, host=YAHOO_HOSTS[0]):
    return "https://%s/v8/finance/chart/%s?range=1d&interval=1d" % (host, b3.yahoo(sym))


def parse_yahoo(text, sym):
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise AlertError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise AlertError("Yahoo has no data for %s: %s" % (sym, err.get("description", "unknown ticker")))
    m = res[0].get("meta", {})
    price = m.get("regularMarketPrice")
    if price is None:
        raise AlertError("Yahoo has no price for %s" % sym)
    t = m.get("regularMarketTime")
    when = b3.iso_utc(b3.utc_of(t)) if t else ""
    q = (res[0].get("indicators", {}).get("quote") or [{}])[0]
    opens = [x for x in (q.get("open") or []) if x is not None]
    return {"symbol": sym, "price": float(price), "open": opens[0] if opens else None,
            "high": m.get("regularMarketDayHigh"), "low": m.get("regularMarketDayLow"),
            "prev_close": m.get("previousClose") or m.get("chartPreviousClose"), "time": when,
            "currency": m.get("currency"), "source": "yahoo"}


_urlopen = urllib.request.urlopen   # tests swap these two
_sleep = time.sleep


def _get(url, timeout=20):
    """(HTTP status, body). A network failure is an AlertError."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with _urlopen(req, timeout=timeout) as r:
            return 200, r.read(MAX_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read(MAX_BYTES).decode("utf-8", "replace")
        except Exception:
            body = ""
        finally:
            try:
                e.close()
            except Exception:
                pass
        return e.code, body
    except urllib.error.URLError as e:
        raise AlertError("could not reach %s: %s" % (url.split("/")[2], e.reason))


def fetch_yahoo(sym, timeout=20):
    """query2 first. A 429 waits BACKOFF seconds and tries once more; a 429 or a 5xx
    after that moves on to query1. A 404 comes back as Yahoo's own error JSON."""
    last = None
    for host in YAHOO_HOSTS:
        url = yahoo_url(sym, host)
        code, text = _get(url, timeout)
        if code == 429:
            _sleep(BACKOFF)
            code, text = _get(url, timeout)
        if code == 200:
            return text
        if code == 404 and text.strip().startswith("{"):
            return text
        last = (url, code)
        if code != 429 and code < 500:
            break
    raise AlertError("%s answered HTTP %d" % (last[0].split("?")[0], last[1]))


def _offline_text(folder, sym):
    base = os.path.join(folder, "yahoo-%s.json" % sym)
    if os.path.exists(base):
        with open(base, encoding="utf-8") as fh:
            return fh.read()
    if os.path.exists(base + ".gz"):
        with gzip.open(base + ".gz", "rt", encoding="utf-8") as fh:
            return fh.read()
    raise AlertError("no offline quote file %s" % base)


def get_quotes(symbols, offline=None, pause=PAUSE):
    """{SYM: quote} and [errors]. One request per ticker, with a pause between.
    PETR4F is quoted as PETR4 and filed under PETR4F."""
    quotes, errors = {}, []
    for i, raw in enumerate(symbols):
        try:
            sym = _sym(raw)
            src = b3.base(sym)
            if offline:
                text = _offline_text(offline, src)
            else:
                if i and pause:
                    _sleep(pause)
                text = fetch_yahoo(src)
            q = parse_yahoo(text, src)
            q["symbol"] = sym
            quotes[sym] = q
        except AlertError as e:
            errors.append(str(e))
    return quotes, errors


def write_prices(path, quotes, now_utc):
    old = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh)
        except (ValueError, OSError):
            old = {}
    prices = dict(old.get("prices", {}))
    detail = dict(old.get("detail", {}))
    stamp = b3.iso_utc(now_utc)
    for s, q in quotes.items():
        prices[s] = q["price"]
        detail[s] = {"prev_close": q.get("prev_close"), "time": q.get("time"), "source": q.get("source"),
                     "fetched": stamp}
    doc = {"as_of": stamp, "source": "yahoo", "currency": "BRL",
           "note": ("Delayed quotes (about 15 minutes for B3) for alerts and sizing checks. Your broker's price "
                    "is the one that fills. detail.time is the quote's own time; detail.fetched is when it was read."),
           "prices": prices, "detail": detail}
    _write(path, doc)
    return doc


def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def _read(path, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise AlertError("%s is not valid JSON: %s" % (path, e))


# ---------------------------------------------------------------- rules

PRICE_TYPES = {"above", "below", "stop", "target", "pct_move", "opening_bell", "time"}
NO_QUOTE = {"opening_bell", "time"}


def _hhmm(text):
    try:
        return b3.parse_hhmm(text)
    except ValueError:
        return None


def validate(rule):
    t = rule.get("type")
    if t not in PRICE_TYPES:
        raise AlertError("rule %s: unknown type %r" % (rule.get("id"), t))
    if t not in NO_QUOTE and not rule.get("symbol"):
        raise AlertError("rule %s needs a symbol" % rule.get("id"))
    if rule.get("symbol") and not b3.is_ticker(rule["symbol"]):
        raise AlertError("rule %s: %r is not a B3 ticker" % (rule.get("id"), rule["symbol"]))
    if t in ("above", "below", "stop", "target") and not isinstance(rule.get("price"), (int, float)):
        raise AlertError("rule %s needs a numeric price" % rule.get("id"))
    if t == "pct_move" and not isinstance(rule.get("pct"), (int, float)):
        raise AlertError("rule %s needs a numeric pct" % rule.get("id"))
    if t == "time" and _hhmm(rule.get("at")) is None:
        raise AlertError("rule %s needs \"at\" as a Sao Paulo time like \"11:45\"" % rule.get("id"))
    if rule.get("date") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(rule["date"])):
        raise AlertError("rule %s: date must be YYYY-MM-DD" % rule.get("id"))


def active(rule, today):
    """A rule with a "date" is watched on that Sao Paulo date only."""
    return not rule.get("date") or str(rule["date"]) == today


def _rang_today(stamp, today):
    """Did a rule that rang at this UTC stamp ring on this Sao Paulo date?"""
    if not stamp:
        return False
    try:
        utc = datetime.datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return stamp[:10] == today
    return b3.to_brt(utc).date().isoformat() == today


def ring_time(rule, status):
    """The time a time rule rings today. "before_close": no later than 10 minutes
    before the end of continuous trading (16:55 or 17:55, see b3.py)."""
    at = _hhmm(rule.get("at"))
    if rule.get("before_close") and status.get("closes"):
        close = datetime.datetime.combine(datetime.date(2000, 1, 1), b3.parse_hhmm(status["closes"]))
        latest = (close - datetime.timedelta(minutes=BEFORE_CLOSE_MIN)).time()
        if at > latest:
            at = latest
    return at


def evaluate(rules, quotes, status, state, now_utc):
    """Pure function. Returns (rings, new_state). Each ring: {id, symbol, message}."""
    state = json.loads(json.dumps(state or {}))
    rung = state.setdefault("rung", {})
    today = status["date"]
    local = b3.to_brt(now_utc)
    stamp = b3.iso_utc(now_utc)
    rings = []
    for r in rules:
        validate(r)
        rid = r.get("id") or "%s-%s" % (r.get("symbol", "market"), r["type"])
        if not active(r, today):
            continue
        typ = {"stop": "below", "target": "above"}.get(r["type"], r["type"])
        if typ == "opening_bell":
            if status["open"] and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": "", "message": "Opening bell: B3 is open (%s Sao Paulo)%s."
                              % (status["now_sao_paulo"], (", " + status["session_note"])
                                 if status.get("session_note") else "")})
                rung[rid] = stamp
            continue
        if typ == "time":
            at = ring_time(r, status)
            if status.get("opens") and local.time() >= at and not _rang_today(rung.get(rid), today):
                label = r.get("label") or "time alert"
                msg = "%s%s: it is %s Sao Paulo (set for %s)" % (label[:1].upper(), label[1:],
                                                                  local.strftime("%H:%M"), b3.hm(at))
                msg += (", continuous trading ends at %s." % status["closes"]) if status["open"] else \
                    ", continuous trading has ended."
                sym = str(r.get("symbol") or "").upper()
                if sym:
                    msg += (" If %s is still open, close it: trader-broker flatten --symbol %s builds the sell "
                            "ticket, then trader-risk and your yes." % (sym, sym))
                rings.append({"id": rid, "symbol": sym, "message": msg})
                rung[rid] = stamp
            continue
        sym = r["symbol"].upper()
        q = quotes.get(sym)
        if not q:
            continue
        p = q["price"]
        if typ in ("above", "below"):
            hit = p >= r["price"] if typ == "above" else p <= r["price"]
            if hit and rid not in rung:
                word = "at or above" if typ == "above" else "at or below"
                label = {"stop": "stop", "target": "target"}.get(r["type"], "alert level")
                rings.append({"id": rid, "symbol": sym, "message": "%s %s %s: %s your %s %s%s."
                              % (sym, "is" if status["open"] else "last", b3.brl(p), word, label,
                                 b3.brl(r["price"]), (" (" + r["note"] + ")") if r.get("note") else "")})
                rung[rid] = stamp
            elif not hit and rid in rung and r.get("rearm"):
                del rung[rid]
        elif typ == "pct_move":
            ref = r.get("ref") or q.get("prev_close") or q.get("open")
            if not ref:
                continue
            move = 100.0 * (p / float(ref) - 1)
            want = r.get("direction")
            hit = abs(move) >= r["pct"] and (not want or (want == "up") == (move > 0))
            if hit and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": sym, "message": "%s moved %+.2f%% to %s (from %s)."
                              % (sym, move, b3.brl(p), b3.brl(float(ref)))})
                rung[rid] = stamp
    state["last_check"] = stamp
    return rings, state


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def rules_from_plan(plan, cfg_flat_by=None):
    """trader/plan.json -> a stop alert, a target alert and a flat-by time alert, for the plan's day."""
    if not isinstance(plan, dict) or not plan.get("symbol"):
        raise AlertError("the plan has no symbol (run trader-exit first)")
    sym = _sym(plan["symbol"])
    out = []
    for key, typ, word in (("stop", "stop", "plan stop"), ("target", "target", "plan target")):
        v = plan.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            raise AlertError("the plan has no %s (run trader-exit)" % key)
        label = plan.get("%s_label" % key)
        out.append({"id": "%s-plan-%s" % (sym.lower(), key), "symbol": sym, "type": typ, "price": float(v),
                    "note": word + (": " + label if label else "")})
    flat = plan.get("flat_by") or cfg_flat_by or "11:45"
    if _hhmm(flat) is None:
        raise AlertError("the plan's flat_by %r is not a time like 11:45" % flat)
    out.append({"id": "%s-flat-by" % sym.lower(), "symbol": sym, "type": "time", "at": flat,
                "label": "flat by", "before_close": True})
    if plan.get("date"):
        for r in out:
            r["date"] = str(plan["date"])
    return out


# ---------------------------------------------------------------- commands

def holdings(a):
    syms = []
    pf = _read(a.portfolio, {})
    for p in pf.get("positions", []) if isinstance(pf, dict) else []:
        syms.append(str(p.get("ticker") or p.get("symbol") or "").upper())
    plan = _read(a.plan, {})
    if isinstance(plan, dict) and plan.get("symbol"):
        syms.append(str(plan["symbol"]).upper())
    return [s for s in syms if s]


def _dedupe(items):
    out = []
    for s in items:
        if s and s not in out:
            out.append(s)
    return out


def cmd_clock(a):
    cfg = _read(a.config, {}) if getattr(a, "config", None) else {}
    st = market_status(parse_now(a.now), cfg)
    if a.json:
        print(json.dumps(st, indent=2))
    else:
        print("Sao Paulo %s (%s): B3 %s, %s." % (st["now_sao_paulo"], st["weekday"],
                                                 "OPEN" if st["open"] else "closed", st["reason"]))
        if st.get("opens"):
            print("Today's session: continuous trading %s to %s, closing call to %s%s." % (
                st["opens"], st["closes"], st["call_ends"],
                ("; " + st["session_note"]) if st.get("session_note") else ""))
        if not st["open"] and st.get("next_open"):
            print("Next open: %s Sao Paulo." % st["next_open"])
        if st["calendar_note"]:
            print("Note: %s." % st["calendar_note"])
    return 0 if st["open"] else 1


def cmd_quote(a):
    syms = _dedupe([_sym(s) for s in a.symbols] + ([_sym(s) for s in holdings(a)] if a.holdings else []))
    if not syms:
        raise AlertError("which tickers? alerts.py quote PETR4 VALE3, or --holdings")
    now = parse_now(a.now)
    quotes, errors = get_quotes(syms, a.offline)
    if a.write and quotes:
        write_prices(a.write, quotes, now)
    if a.json:
        print(json.dumps({"quotes": quotes, "errors": errors}, indent=2))
    else:
        for s in syms:
            q = quotes.get(s)
            if q:
                chg = ""
                if q.get("prev_close"):
                    chg = " (%+.2f%% vs prev close %s)" % (100.0 * (q["price"] / q["prev_close"] - 1),
                                                           b3.brl(q["prev_close"]))
                when = ""
                if q.get("time"):
                    when = b3.to_brt(b3.parse_utc(q["time"])).strftime("%Y-%m-%d %H:%M") + " Sao Paulo"
                print("%-7s %s%s  %s  [%s]" % (s, b3.brl(q["price"]), chg, when, q["source"]))
        for e in errors:
            print("error: " + e)
        if a.write and quotes:
            print("Wrote %d price(s) to %s." % (len(quotes), a.write))
        print("Free Yahoo quotes for B3 are delayed about 15 minutes: for alerts only. Your broker's price is "
              "the one that fills.")
    return 0 if quotes else 1


def cmd_check(a):
    doc = _read(a.rules, None)
    if not doc or not doc.get("rules"):
        raise AlertError("no rules in %s: add some with alerts.py add --from-plan" % a.rules)
    rules = doc["rules"]
    now = parse_now(a.now)
    cfg = _read(a.config, {}) if a.config else {}
    status = market_status(now, cfg)
    watched = [r for r in rules if active(r, status["date"])]
    syms = _dedupe([str(r.get("symbol", "")).upper() for r in watched if r.get("type") not in NO_QUOTE])
    quotes, errors = (get_quotes(syms, a.offline) if syms else ({}, []))
    state = _read(a.state, {})
    rings, state = evaluate(rules, quotes, status, state, now)
    if not a.no_write:
        _write(a.state, state)
        if quotes:
            write_prices(a.prices_out, quotes, now)
    if a.json:
        print(json.dumps({"market": status, "rings": rings, "errors": errors,
                          "quotes": {s: q["price"] for s, q in quotes.items()}}, indent=2))
    else:
        print("B3 %s (%s Sao Paulo, %s)." % ("OPEN" if status["open"] else "closed", status["now_sao_paulo"],
                                             status["reason"]))
        for r in rings:
            print("ALERT " + r["message"])
        if not rings:
            print("Nothing hit. %d rule(s) watched today%s." % (
                len(watched), (", " + ", ".join("%s %s" % (s, b3.brl(quotes[s]["price"])) for s in syms
                                                if s in quotes)) if quotes else ""))
        for e in errors:
            print("error: " + e)
        if rings:
            print("An alert is information, not an order, and the quote behind it is about 15 minutes old. "
                  "Check the live price and your OCO stop / target in your broker's platform, and record any "
                  "fill (ticket.py fill). To close a position: ticket.py flatten, then trader-risk and your yes.")
    return 0 if rings else 1


def cmd_add(a):
    doc = _read(a.rules, {"rules": []})
    doc.setdefault("rules", [])
    if a.from_plan:
        plan = _read(a.plan, None)
        if plan is None:
            raise AlertError("no plan at %s (run trader-exit first)" % a.plan)
        cfg = _read(a.config, {}) if a.config else {}
        new = rules_from_plan(plan, (cfg or {}).get("flat_by"))
        ids = {r["id"] for r in new}
        doc["rules"] = [r for r in doc["rules"] if r.get("id") not in ids] + new
        state = _read(a.state, {})
        if state.get("rung"):
            for i in ids:
                state["rung"].pop(i, None)
            _write(a.state, state)
        _write(a.rules, doc)
        for r in new:
            print("Added %s: %s" % (r["id"], json.dumps(r, ensure_ascii=False)))
        print("Run alerts.py check every few minutes while the trade is open (a scheduled task or /loop). "
              "Free B3 quotes are about 15 minutes late; the OCO stop and target at your broker are what "
              "actually get you out.")
        return 0
    if a.opening_bell:
        rule = {"id": a.id or "opening-bell", "type": "opening_bell"}
    elif a.at:
        if _hhmm(a.at) is None:
            raise AlertError("--at wants a Sao Paulo time like 11:45")
        rule = {"id": a.id or ("time-%s" % a.at.replace(":", "")), "type": "time", "at": a.at,
                "label": a.label or "time alert"}
        if a.symbol:
            rule["symbol"] = _sym(a.symbol)
        if a.before_close:
            rule["before_close"] = True
    else:
        if not a.symbol:
            raise AlertError("--symbol is required (or --from-plan, --at, --opening-bell)")
        sym = _sym(a.symbol)
        if a.above is not None:
            rule = {"type": "above", "price": a.above}
        elif a.below is not None:
            rule = {"type": "below", "price": a.below}
        elif a.stop is not None:
            rule = {"type": "stop", "price": a.stop}
        elif a.target is not None:
            rule = {"type": "target", "price": a.target}
        elif a.pct is not None:
            rule = {"type": "pct_move", "pct": a.pct}
            if a.direction:
                rule["direction"] = a.direction
        else:
            raise AlertError("give one of --above, --below, --stop, --target or --pct")
        val = rule.get("price", rule.get("pct"))
        rule = dict({"id": a.id or ("%s-%s-%s" % (sym.lower(), rule["type"], _n(val)))}, symbol=sym, **rule)
        if a.rearm:
            rule["rearm"] = True
        if a.note:
            rule["note"] = a.note
    if a.date:
        rule["date"] = a.date
    validate(rule)
    if any(r.get("id") == rule["id"] for r in doc["rules"]):
        raise AlertError("a rule with id %s already exists" % rule["id"])
    doc["rules"].append(rule)
    _write(a.rules, doc)
    print("Added %s: %s" % (rule["id"], json.dumps(rule, ensure_ascii=False)))
    return 0


def cmd_list(a):
    doc = _read(a.rules, {"rules": []})
    state = _read(a.state, {})
    if not doc.get("rules"):
        print("No rules yet.")
        return 1
    for r in doc["rules"]:
        rid = r.get("id")
        rung = state.get("rung", {}).get(rid)
        print("%-24s %s%s" % (rid, json.dumps({k: v for k, v in r.items() if k != "id"}, ensure_ascii=False),
                              ("  (rang %s)" % rung) if rung else ""))
    return 0


def cmd_set_price(a):
    sym = _sym(a.symbol)
    price = b3.parse_number(a.price)
    if price is None or price <= 0:
        raise AlertError("set-price wants a price above 0, like 44.12 or 44,12")
    now = parse_now(a.now)
    when = now
    if a.time:
        t = _hhmm(a.time[:5])
        if t is None:
            raise AlertError("--time wants the Sao Paulo time the price was read, like 10:31")
        when = b3.brt_to_utc(datetime.datetime.combine(b3.to_brt(now).date(), t))
        if when > now + datetime.timedelta(minutes=1):
            raise AlertError("--time %s is in the future" % a.time)
    if not 0 < len(a.source) <= 40 or not re.match(r"^[A-Za-z0-9 ._-]+$", a.source):
        raise AlertError("--source is a short label like xp-browser")
    old = _read(a.write, {}) or {}
    last = (old.get("prices") or {}).get(sym)
    if last and not a.force and abs(price / float(last) - 1) > 0.20:
        raise AlertError("%s %s is %.0f%% away from the last known %s: check the reading, or pass --force"
                         % (sym, b3.brl(price), 100 * abs(price / float(last) - 1), b3.brl(float(last))))
    q = {sym: {"symbol": sym, "price": price, "prev_close": ((old.get("detail") or {}).get(sym) or {}).get("prev_close"),
               "time": b3.iso_utc(when), "source": a.source}}
    write_prices(a.write, q, now)
    doc = _read(a.write, {})
    doc["source"] = "mixed" if doc.get("source") not in (None, a.source) else a.source
    _write(a.write, doc)
    print("Recorded %s %s from %s, read at %s Sao Paulo, in %s. It counts as fresh for 20 minutes."
          % (sym, b3.brl(price), a.source, b3.to_brt(when).strftime("%H:%M"), a.write))
    return 0


def cmd_reset(a):
    state = _read(a.state, {})
    if a.id not in state.get("rung", {}):
        print("%s has not rung (nothing to reset)." % a.id)
        return 1
    del state["rung"][a.id]
    _write(a.state, state)
    print("Re-armed %s." % a.id)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Price alerts and the B3 clock (trader-alerts).")
    sub = ap.add_subparsers(dest="cmd")

    def net(p):
        p.add_argument("--offline", metavar="DIR", help="read yahoo-<TICKER>.json files, no network")
        p.add_argument("--now", help="pretend it is this time (no offset = Sao Paulo time)")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("clock", help="is B3 open?")
    p.add_argument("--now")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("quote", help="latest delayed quotes")
    net(p)
    p.add_argument("symbols", nargs="*")
    p.add_argument("--holdings", action="store_true", help="add tickers from your portfolio and plan")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--write", nargs="?", const="trader/prices.json", metavar="PATH",
                   help="save to prices.json (default trader/prices.json)")
    p = sub.add_parser("check", help="check every rule, ring what hit")
    net(p)
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--prices-out", default="trader/prices.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--no-write", action="store_true")
    p = sub.add_parser("add", help="add a rule")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--symbol")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--above", type=float)
    g.add_argument("--below", type=float)
    g.add_argument("--stop", type=float)
    g.add_argument("--target", type=float)
    g.add_argument("--pct", type=float)
    g.add_argument("--opening-bell", action="store_true")
    g.add_argument("--at", metavar="HH:MM", help="a time alert, Sao Paulo time")
    g.add_argument("--from-plan", action="store_true", help="stop, target and flat-by from the plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json", help="flat_by when the plan has none")
    p.add_argument("--label", help="what a time alert says, e.g. \"flat by\"")
    p.add_argument("--before-close", action="store_true",
                   help="ring no later than 10 minutes before continuous trading ends")
    p.add_argument("--date", help="watch this rule on this Sao Paulo date only (YYYY-MM-DD)")
    p.add_argument("--direction", choices=["up", "down"])
    p.add_argument("--rearm", action="store_true")
    p.add_argument("--note")
    p.add_argument("--id")
    p = sub.add_parser("list", help="list the rules")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p = sub.add_parser("set-price", help="record a live price you read in your broker (e.g. XP in the browser)")
    p.add_argument("symbol")
    p.add_argument("price")
    p.add_argument("--source", default="xp-browser")
    p.add_argument("--time", help="Sao Paulo time the price was read (default: now)")
    p.add_argument("--write", default="trader/prices.json")
    p.add_argument("--now")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("reset", help="re-arm a rule that rang")
    p.add_argument("id")
    p.add_argument("--state", default="trader/state/alerts.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"clock": cmd_clock, "quote": cmd_quote, "check": cmd_check, "add": cmd_add,
                "list": cmd_list, "reset": cmd_reset, "set-price": cmd_set_price}[a.cmd](a)
    except AlertError as e:
        print("alerts.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
