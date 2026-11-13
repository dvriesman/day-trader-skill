#!/usr/bin/env python3
"""Insider trades for trader-insiders: what a B3 company's own controllers,
board members and officers bought and sold. Standard library only.

    python3 insiders.py company PETR4              # month by month, by group, last 6 months
    python3 insiders.py company PETR4 VALE3 --months 12
    python3 insiders.py top                        # biggest net insider buys and sells, newest month
    python3 insiders.py top --month 2026-09 --min-brl 1000000 --json

The law: CVM Resolution 44 (art. 11) makes every listed company report, each
month, every trade in its shares by its controllers, board (Conselho de
Administracao), officers (Diretoria), fiscal council and other statutory
bodies, and by people tied to them (spouses, dependants, companies they
control). The company files the consolidated report by the 10th of the next
month. CVM publishes it as open data: "Valores Mobiliarios Negociados e
Detidos", vlmo_cia_aberta_<YEAR>.zip (dados.cvm.gov.br, about 0.6 MB), with
the group, the kind of move, the date, the quantity and the price. No names:
the report is by group, so this tool never shows a person.

What counts as a trade here: an open-market purchase or sale ("Compra a
vista", "Venda a vista", "Compra", "Venda") of shares or units. Grants from a
pay plan, option exercises, lending, subscriptions, splits, gifts and
derivatives are left out of the totals (they say little about what an
insider thinks the price is worth). The company itself buying back its shares
(Tipo_Empresa "Companhia" with no group) is shown apart, as "buyback".

Lag: a trade on the 2nd of a month is public around the 10th of the next one,
up to 40 days later. These are slow signals, context for a day trade, never a
trigger for one. --offline DIR reads vlmo-<YEAR>.zip and fca.zip from DIR.
"""

import argparse
import datetime as _dt
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import b3  # noqa: E402

UA = "Mozilla/5.0 (compatible; b3-day-trader-skill/2.0)"
CACHE_DAYS = 1
MAX_DOWNLOAD = 40 * 1024 * 1024
BUY = {"compra a vista", "compra"}
SELL = {"venda a vista", "venda"}
ASSETS = {"acoes", "units"}
GROUPS = [("controlador", "controllers"), ("conselho de administracao", "board"), ("diretor", "officers"),
          ("conselho fiscal", "fiscal council"), ("orgao estatutario", "other statutory bodies")]


class InsiderError(Exception):
    pass


def fetch(kind, year, cache, offline, log):
    name = "fca.zip" if kind == "fca" else "%s-%d.zip" % (kind, year)
    if offline:
        p = os.path.join(offline, name)
        if not os.path.isfile(p):
            raise InsiderError("offline: no file %s" % p)
        return p
    os.makedirs(cache, exist_ok=True)
    p = os.path.join(cache, "%s-%d.zip" % (kind, year))
    if os.path.isfile(p) and time.time() - os.path.getmtime(p) < CACHE_DAYS * 86400:
        return p
    url = b3.CVM_DATA % (kind.upper(), kind, year)
    log("Downloading %s (CVM open data)..." % url.rsplit("/", 1)[-1])
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read(MAX_DOWNLOAD + 1)
    except urllib.error.HTTPError as e:
        if os.path.isfile(p):
            return p
        raise InsiderError("dados.cvm.gov.br answered HTTP %d for %s" % (e.code, url.rsplit("/", 1)[-1]))
    except urllib.error.URLError as e:
        if os.path.isfile(p):
            return p
        raise InsiderError("could not reach dados.cvm.gov.br: %s" % e.reason)
    if len(data) > MAX_DOWNLOAD or data[:2] != b"PK":
        raise InsiderError("%s is not the zip CVM publishes: refused" % url.rsplit("/", 1)[-1])
    tmp = p + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, p)
    return p


def group_of(r):
    cargo = b3.fold(r.get("Tipo_Cargo"))
    for key, name in GROUPS:
        if cargo.startswith(key):
            return name
    if b3.fold(r.get("Tipo_Empresa")) == "companhia" and not cargo:
        return "buyback"
    return None


def _num(x):
    try:
        return float(str(x).replace(",", "."))
    except (TypeError, ValueError):
        return None


def trades(vlmo_zip, cnpjs=None):
    """Open-market buys and sells of shares and units, one dict per reported move."""
    out = []
    try:
        rows = list(b3.cvm_csv_rows(vlmo_zip, r"vlmo_cia_aberta_con_\d{4}\.csv$"))
    except ValueError as e:
        raise InsiderError("the VLMO file: %s" % e)
    best = {}
    for r in rows:   # the latest version of each company's monthly report wins
        k = (r.get("CNPJ_Companhia"), r.get("Data_Referencia"))
        v = int(r.get("Versao") or 0)
        best[k] = max(best.get(k, 0), v)
    for r in rows:
        if cnpjs is not None and r.get("CNPJ_Companhia") not in cnpjs:
            continue
        if int(r.get("Versao") or 0) != best[(r.get("CNPJ_Companhia"), r.get("Data_Referencia"))]:
            continue
        move = b3.fold(r.get("Tipo_Movimentacao"))
        if move not in BUY and move not in SELL:
            continue
        if b3.fold(r.get("Tipo_Ativo")) not in ASSETS:
            continue
        g = group_of(r)
        if g is None:
            continue
        q, p, vol = _num(r.get("Quantidade")), _num(r.get("Preco_Unitario")), _num(r.get("Volume"))
        if not q:
            continue
        if vol is None and p is not None:
            vol = q * p
        out.append({"cnpj": r.get("CNPJ_Companhia"), "company": " ".join((r.get("Nome_Companhia") or "").split()),
                    "month": (r.get("Data_Referencia") or "")[:7], "date": r.get("Data_Movimentacao") or "",
                    "group": g, "side": "buy" if move in BUY else "sell", "shares": q, "price": p,
                    "value": vol or 0.0, "class": (r.get("Caracteristica_Valor_Mobiliario") or "").strip()[:30]})
    return out


def summarize(rows):
    """{month: {group: {"buy": brl, "sell": brl, "net": brl, "n": moves}}}"""
    out = {}
    for t in rows:
        g = out.setdefault(t["month"], {}).setdefault(t["group"], {"buy": 0.0, "sell": 0.0, "n": 0})
        g[t["side"]] += t["value"]
        g["n"] += 1
    for m in out.values():
        for g in m.values():
            g["net"] = g["buy"] - g["sell"]
    return out


def big(v):
    a = abs(v)
    for div, unit in ((1e9, "bi"), (1e6, "mi"), (1e3, "mil")):
        if a >= div:
            return "%sR$ %s %s" % ("-" if v < 0 else "", b3._br(a / div, 1), unit)
    return b3.brl(v)


def cmd_company(a, tmap, vlmo_paths):
    out = []
    for t in a.tickers:
        try:
            sym = b3.base(t)
        except ValueError as e:
            raise InsiderError(str(e))
        info = tmap.get(sym)
        if not info:
            raise InsiderError("%s is not in CVM's list of listed companies" % sym)
        rows = []
        for p in vlmo_paths:
            rows.extend(trades(p, {info["cnpj"]}))
        months = sorted(summarize(rows).items())[-a.months:]
        out.append({"ticker": sym, "name": info["name"], "months": [{"month": m, "groups": g} for m, g in months]})
    if a.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    for c in out:
        print("%s  %s: open-market trades by its own insiders (CVM, RCVM 44 art. 11), by month of the report." % (
            c["ticker"], c["name"]))
        if not c["months"]:
            print("  No open-market insider buys or sells reported in the data.")
        for m in c["months"]:
            parts = []
            for g, v in sorted(m["groups"].items(), key=lambda kv: -abs(kv[1]["net"])):
                parts.append("%s %s net (bought %s, sold %s)" % (g, big(v["net"]), big(v["buy"]), big(v["sell"])))
            print("  %s  %s" % (m["month"], "; ".join(parts)))
        print("")
    print("Reported by the 10th of the next month: up to 40 days old. Context, never a trigger. Not financial advice.")
    return 0


def cmd_top(a, tmap, vlmo_paths):
    rows = []
    for p in vlmo_paths:
        rows.extend(trades(p))
    months = sorted({t["month"] for t in rows})
    if not months:
        raise InsiderError("no insider trades in the data")
    counts = {}
    for t in rows:
        counts.setdefault(t["month"], set()).add(t["cnpj"])
    month = a.month or months[-1]
    if not a.month and len(months) > 1 and len(counts[months[-1]]) < 0.5 * len(counts[months[-2]]):
        month = months[-2]   # the newest month is still coming in (reports are due by the 10th)
    if month not in months:
        raise InsiderError("no reports for %s (the data has %s to %s)" % (month, months[0], months[-1]))
    by_cnpj = {}
    for t in rows:
        if t["month"] != month or t["group"] == "buyback":
            continue
        c = by_cnpj.setdefault(t["cnpj"], {"company": t["company"], "buy": 0.0, "sell": 0.0, "groups": set()})
        c[t["side"]] += t["value"]
        c["groups"].add(t["group"])
    tick = {}
    for sym, info in tmap.items():
        tick.setdefault(info.get("cnpj"), []).append(sym)
    ranked = []
    for cnpj, c in by_cnpj.items():
        net = c["buy"] - c["sell"]
        if abs(net) < a.min_brl:
            continue
        ranked.append({"tickers": sorted(tick.get(cnpj, [])), "company": c["company"], "net": net,
                       "buy": c["buy"], "sell": c["sell"], "groups": sorted(c["groups"])})
    ranked.sort(key=lambda r: -r["net"])
    if a.json:
        print(json.dumps({"month": month, "companies": ranked}, indent=2, ensure_ascii=False))
        return 0 if ranked else 1
    print("Insider open-market trades reported for %s (CVM VLMO), net of at least %s:" % (month, big(a.min_brl)))
    buys = [r for r in ranked if r["net"] > 0][:a.show]
    sells = [r for r in reversed(ranked) if r["net"] < 0][:a.show]
    for title, rows_ in (("Net buyers", buys), ("Net sellers", sells)):
        print(title + ":")
        if not rows_:
            print("  none")
        for r in rows_:
            print("  %-14s %-34s %12s  (%s)" % (",".join(r["tickers"][:2]) or "-", r["company"][:34], big(r["net"]),
                                               ", ".join(r["groups"])))
    print("Up to 40 days old by the time it is public. Context, never a trigger. Not financial advice.")
    return 0 if ranked else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Insider trades in B3 companies, from CVM (trader-insiders).")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--cache", default="trader/state/cvm")
        p.add_argument("--offline", metavar="DIR")
        p.add_argument("--now", help="pretend it is this Sao Paulo time")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("company", help="month by month, by group, for your tickers")
    p.add_argument("tickers", nargs="+")
    p.add_argument("--months", type=int, default=6)
    common(p)
    p = sub.add_parser("top", help="the biggest net insider buys and sells of one month, market-wide")
    p.add_argument("--month", help="YYYY-MM (default: the newest in the data)")
    p.add_argument("--min-brl", type=float, default=500000)
    p.add_argument("--show", type=int, default=15)
    common(p)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        now = b3.parse_now(a.now)
        year = b3.to_brt(now).year
        log = (lambda m: None) if a.json else (lambda m: print(m, file=sys.stderr))
        try:
            fca = fetch("fca", year, a.cache, a.offline, log)
        except InsiderError:
            if a.offline:
                raise
            fca = fetch("fca", year - 1, a.cache, a.offline, log)
        try:
            tmap = b3.fca_ticker_map(fca)
        except ValueError as e:
            raise InsiderError("the CVM company list: %s" % e)
        paths = []
        for y in (year - 1, year):
            try:
                paths.append(fetch("vlmo", y, a.cache, a.offline, log))
            except InsiderError:
                if y == year and not paths:
                    raise
        return {"company": cmd_company, "top": cmd_top}[a.cmd](a, tmap, paths)
    except (InsiderError, ValueError) as e:
        print("insiders.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
