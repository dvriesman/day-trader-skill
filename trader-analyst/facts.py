#!/usr/bin/env python3
"""Earnings reader for trader-analyst (B3 companies). Standard library only.

Pulls a company's reported numbers from CVM's open data (the ITR quarterly
statements and the DFP annual statements every listed company files) and lays
out what changed, quarter by quarter: revenue, gross profit, EBIT, net income,
gross / EBIT / net margins, EPS, and growth against the same quarter a year
earlier.

    python3 facts.py PETR4
    python3 facts.py PETR4 VALE3 --quarters 8
    python3 facts.py PETR4 --json
    python3 facts.py PETR4 VALE3 PRIO3 --metrics trader/metrics.json

Sources (dados.cvm.gov.br, no key, no sign-up):
  FCA  fca_cia_aberta_<YEAR>.zip   ticker -> company (CVM code)       ~0.4 MB
  ITR  itr_cia_aberta_<YEAR>.zip   quarterly statements, this year    ~25 MB
  DFP  dfp_cia_aberta_<YEAR>.zip   annual statements, last year       ~15 MB
  The zips are cached in trader/state/cvm/ and fetched again after 3 days.
  CVM refreshes these files about once a week: a report filed in the last
  few days may not be in them yet. The tool says which quarter is the
  newest, and /trader-news lists the newest filings (fatos relevantes,
  "Desempenho 3T26") straight from CVM's RAD the minute they are delivered.

How the quarters are built (consolidated statements, DRE):
  - Accounts: 3.01 revenue (Receita de Venda de Bens e/ou Servicos; for a
    bank, Receitas de Intermediacao Financeira), 3.03 gross profit, 3.05 EBIT
    (Resultado Antes do Resultado Financeiro e dos Tributos), 3.11 net income
    (Lucro/Prejuizo Consolidado do Periodo), 3.99.01.01 basic EPS of the
    common share (ON). Banks and insurers report differently: their gross and
    EBIT lines can be blank.
  - A quarter is a period of about three months. The ITR repeats last year's
    same quarter as a comparison ("PENULTIMO"): that is where the year-ago
    numbers come from. The latest version of a filing wins.
  - Companies file no fourth-quarter ITR. Q4 is the full year (DFP) minus the
    three quarters inside it, marked "derived"; derived EPS is approximate.
  - Values are in reais (ESCALA_MOEDA MIL is multiplied by 1,000).

--metrics FILE merges one row per ticker into a JSON file. Offline: --offline
DIR reads fca.zip, itr-<YEAR>.zip and dfp-<YEAR>.zip from DIR.

Numbers only: this tool never says whether a stock is a buy.
"""

import argparse
import datetime as _dt
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
CACHE_DAYS = 3
MAX_DOWNLOAD = 120 * 1024 * 1024
ACCOUNTS = {"3.01": "revenue", "3.03": "gross_profit", "3.05": "ebit", "3.11": "net_income",
            "3.99.01.01": "eps"}
NET_INCOME = "lucro/prejuizo consolidado do periodo"   # 3.11 for most companies, 3.09 for banks


def metric_of(r):
    """The metric a DRE row carries, or None."""
    acct = (r.get("CD_CONTA") or "").strip()
    if acct.count(".") == 1 and b3.fold(r.get("DS_CONTA")).startswith(NET_INCOME):
        return "net_income"
    if acct == "3.11":
        return None                       # 3.11 that is not net income (a bank's 3.11 is something else)
    return ACCOUNTS.get(acct)
FLOWS = ("revenue", "gross_profit", "ebit", "net_income")


class FactsError(Exception):
    pass


# ---------------------------------------------------------------- data files

class Data(object):
    """CVM zips, from the cache, the network or an offline folder."""

    def __init__(self, cache="trader/state/cvm", offline=None, now=None, log=None):
        self.cache, self.offline, self.now = cache, offline, now or b3.utc_now()
        self.log = log or (lambda msg: None)

    def path(self, kind, year):
        """Local path of the <kind> zip for <year> (kind: fca, itr, dfp), downloading it if needed."""
        name = "fca.zip" if kind == "fca" else "%s-%d.zip" % (kind, year)
        if self.offline:
            p = os.path.join(self.offline, name)
            if not os.path.isfile(p):
                raise FactsError("offline: no file %s" % p)
            return p
        os.makedirs(self.cache, exist_ok=True)
        p = os.path.join(self.cache, "%s-%d.zip" % (kind, year))
        if os.path.isfile(p) and time.time() - os.path.getmtime(p) < CACHE_DAYS * 86400:
            return p
        url = b3.CVM_DATA % (kind.upper(), kind, year)
        self.log("Downloading %s (CVM open data)..." % url.rsplit("/", 1)[-1])
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = r.read(MAX_DOWNLOAD + 1)
        except urllib.error.HTTPError as e:
            if os.path.isfile(p):
                self.log("CVM answered HTTP %d: using the cached copy" % e.code)
                return p
            raise FactsError("dados.cvm.gov.br answered HTTP %d for %s" % (e.code, url.rsplit("/", 1)[-1]))
        except urllib.error.URLError as e:
            if os.path.isfile(p):
                return p
            raise FactsError("could not reach dados.cvm.gov.br: %s" % e.reason)
        if len(data) > MAX_DOWNLOAD or data[:2] != b"PK":
            raise FactsError("%s is not the zip CVM publishes: refused" % url.rsplit("/", 1)[-1])
        tmp = p + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, p)
        return p


def ticker_map(data, year):
    for y in (year, year - 1):
        try:
            return b3.fca_ticker_map(data.path("fca", y))
        except FactsError:
            if data.offline or y != year:
                raise
        except ValueError as e:
            raise FactsError("the CVM company list: %s" % e)
    raise FactsError("no CVM company list")


def dre_rows(zip_path, kind, year, codes):
    """Consolidated DRE rows of the companies in `codes` (CVM codes), latest version per filing."""
    pattern = r"%s_cia_aberta_DRE_con_%d\.csv$" % (kind, year)
    best = {}
    try:
        for r in b3.cvm_csv_rows(zip_path, pattern):
            code = (r.get("CD_CVM") or "").strip().zfill(6)
            if code not in codes:
                continue
            m = metric_of(r)
            if m is None:
                continue
            key = (code, r["DT_REFER"], r.get("DT_INI_EXERC") or "", r["DT_FIM_EXERC"], m)
            ver = int(r.get("VERSAO") or 0)
            if key not in best or ver >= best[key][0]:
                best[key] = (ver, r)
    except ValueError as e:
        raise FactsError("%s %d: %s" % (kind.upper(), year, e))
    return [r for _, r in best.values()]


def value_of(r):
    v = float(r["VL_CONTA"])
    if r["CD_CONTA"] == "3.99.01.01":
        return v if v else None           # some companies leave EPS at 0: missing, not zero
    if (r.get("ESCALA_MOEDA") or "").upper() == "MIL":
        v *= 1000.0
    return v


# ---------------------------------------------------------------- quarters

def _d(s):
    return _dt.date.fromisoformat(s)


def build_periods(rows):
    """rows -> {(start, end): {metric: value}} using the latest filing for each period."""
    per, stamp = {}, {}
    for r in rows:
        start, end = r.get("DT_INI_EXERC") or "", r["DT_FIM_EXERC"]
        if not start:
            continue
        k = (start, end)
        m = metric_of(r)
        ref = r["DT_REFER"]
        if (k, m) in stamp and stamp[(k, m)] > ref:
            continue
        stamp[(k, m)] = ref
        v = value_of(r)
        if v is not None:
            per.setdefault(k, {})[m] = v
    return per


def quarters_from(periods):
    """{(start, end): values} -> {end: quarter dict}; Q4 derived from a full year minus 9 months."""
    q = {}
    for (s, e), v in periods.items():
        days = (_d(e) - _d(s)).days
        if 80 <= days <= 100:
            q[e] = dict(v, end=e, start=s, derived=False)
    for (s, e), v in periods.items():
        days = (_d(e) - _d(s)).days
        if days < 350 or e in q:
            continue
        nine = periods.get((s, (_d(s).replace(month=9, day=30)).isoformat()))
        if nine is None:
            ends = [x for x in q if s < x < e]
            if len(ends) == 3:
                nine = {m: sum(q[x].get(m, 0) for x in ends) for m in ACCOUNTS.values()
                        if all(m in q[x] for x in ends)}
        if nine is None:
            continue
        d = {m: v[m] - nine[m] for m in ACCOUNTS.values() if m in v and m in nine}
        if d:
            q[e] = dict(d, end=e, start=(_d(s).replace(month=10, day=1)).isoformat(), derived=True)
    return q


def label(end):
    d = _d(end)
    return "%dT%02d" % ((d.month - 1) // 3 + 1, d.year % 100)


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return 100.0 * (a / abs(b) - 1) if b > 0 else None


def margin(a, rev):
    if a is None or not rev:
        return None
    return 100.0 * a / rev


def analyse(ticker, info, data, now, n_quarters=5):
    year = b3.to_brt(now).year
    code = info["cvm"]
    rows = []
    errors = []
    for kind, y in (("itr", year), ("dfp", year - 1), ("itr", year - 1)):
        try:
            rows.extend(dre_rows(data.path(kind, y), kind, y, {code}))
        except FactsError as e:
            errors.append(str(e))
    q = quarters_from(build_periods(rows))
    if not q:
        raise FactsError("%s (%s): no quarterly statements found in CVM's open data%s"
                         % (ticker, info["name"], ("; " + "; ".join(errors)) if errors else ""))
    ends = sorted(q)
    out = []
    for e in ends[-n_quarters:]:
        cur = q[e]
        prev = q.get(_d(e).replace(year=_d(e).year - 1).isoformat())
        row = {"quarter": label(e), "end": e, "derived": cur["derived"]}
        for m in ACCOUNTS.values():
            row[m] = cur.get(m)
            row[m + "_yoy"] = pct(cur.get(m), prev.get(m)) if prev else None
        row["gross_margin"] = margin(cur.get("gross_profit"), cur.get("revenue"))
        row["ebit_margin"] = margin(cur.get("ebit"), cur.get("revenue"))
        row["net_margin"] = margin(cur.get("net_income"), cur.get("revenue"))
        out.append(row)
    ttm = None
    if len(ends) >= 4:
        last4 = [q[e] for e in ends[-4:]]
        ttm = {m: sum(x.get(m) or 0 for x in last4) for m in ("revenue", "net_income", "eps")
               if all(x.get(m) is not None for x in last4)}
    return {"ticker": ticker, "name": info["name"], "cvm": code, "cnpj": info.get("cnpj"),
            "quarters": out, "ttm": ttm, "latest": label(ends[-1]), "latest_end": ends[-1],
            "notes": errors, "source": "CVM ITR/DFP open data (dados.cvm.gov.br)"}


# ---------------------------------------------------------------- output

def big(v):
    if v is None:
        return "-"
    a = abs(v)
    for div, unit in ((1e9, "bi"), (1e6, "mi"), (1e3, "mil")):
        if a >= div:
            return "R$ %s %s" % (b3._br(v / div, 1), unit)
    return b3.brl(v)


def p1(v):
    return "-" if v is None else "%+.1f%%" % v


def m1(v):
    return "-" if v is None else "%.1f%%" % v


def report(res):
    lines = ["%s  %s (CVM %s). Consolidated, from CVM's ITR/DFP open data." % (res["ticker"], res["name"],
                                                                              res["cvm"])]
    lines.append("%-6s %13s %8s %13s %8s %13s %8s %7s %7s %7s %7s" % (
        "", "revenue", "yoy", "EBIT", "yoy", "net income", "yoy", "gross", "EBIT", "net", "EPS"))
    for r in res["quarters"]:
        lines.append("%-6s %13s %8s %13s %8s %13s %8s %7s %7s %7s %7s" % (
            r["quarter"] + ("*" if r["derived"] else ""), big(r["revenue"]), p1(r["revenue_yoy"]), big(r["ebit"]),
            p1(r["ebit_yoy"]), big(r["net_income"]), p1(r["net_income_yoy"]), m1(r["gross_margin"]),
            m1(r["ebit_margin"]), m1(r["net_margin"]), "-" if r["eps"] is None else b3.px(r["eps"])))
    if any(r["derived"] for r in res["quarters"]):
        lines.append("* fourth quarter = the full year (DFP) minus nine months; derived EPS is approximate.")
    if res.get("ttm"):
        t = res["ttm"]
        lines.append("Last four quarters: revenue %s, net income %s%s." % (
            big(t.get("revenue")), big(t.get("net_income")),
            (", EPS %s" % b3.px(t["eps"])) if t.get("eps") is not None else ""))
    lines.append("Newest quarter in CVM's open data: %s (ends %s). CVM refreshes these files about weekly: a "
                 "report filed in the last days shows first in /trader-news (CVM RAD)." % (res["latest"],
                                                                                          res["latest_end"]))
    for n in res.get("notes") or []:
        lines.append("note: " + n)
    return "\n".join(lines)


def metrics_row(res):
    last = res["quarters"][-1]
    t = res.get("ttm") or {}
    return {"ticker": res["ticker"], "name": res["name"], "cvm": res["cvm"], "period_end": res["latest_end"],
            "revenue_ttm": t.get("revenue"), "net_income_ttm": t.get("net_income"), "eps_ttm": t.get("eps"),
            "revenue_growth_yoy": last["revenue_yoy"], "net_income_growth_yoy": last["net_income_yoy"],
            "gross_margin": last["gross_margin"], "ebit_margin": last["ebit_margin"],
            "net_margin": last["net_margin"], "source": res["source"]}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Quarterly numbers of B3 companies from CVM (trader-analyst).")
    ap.add_argument("tickers", nargs="+")
    ap.add_argument("--quarters", type=int, default=5)
    ap.add_argument("--cache", default="trader/state/cvm")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help="pretend it is this Sao Paulo time")
    ap.add_argument("--metrics", metavar="FILE", help="merge one row per ticker into this JSON file")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        now = b3.parse_now(a.now)
        tickers = []
        for t in a.tickers:
            try:
                tickers.append(b3.base(t))
            except ValueError as e:
                raise FactsError(str(e))
        data = Data(a.cache, a.offline, now, log=(lambda m: None) if a.json else
                    (lambda m: print(m, file=sys.stderr)))
        tmap = ticker_map(data, b3.to_brt(now).year)
        results, errors = [], []
        for t in tickers:
            info = tmap.get(t)
            if not info:
                errors.append("%s is not in CVM's list of listed companies (an ETF, a BDR or a new code?)" % t)
                continue
            try:
                results.append(analyse(t, info, data, now, max(1, a.quarters)))
            except FactsError as e:
                errors.append(str(e))
    except (FactsError, ValueError) as e:
        print("facts.py: %s" % e, file=sys.stderr)
        return 1
    if a.metrics and results:
        doc = {}
        if os.path.isfile(a.metrics):
            try:
                with open(a.metrics, encoding="utf-8") as fh:
                    doc = json.load(fh)
            except ValueError:
                doc = {}
        rows = {r["ticker"]: r for r in doc.get("companies", [])}
        for res in results:
            rows[res["ticker"]] = metrics_row(res)
        doc = {"as_of": b3.to_brt(now).date().isoformat(), "companies": [rows[k] for k in sorted(rows)]}
        d = os.path.dirname(a.metrics)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.metrics, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    if a.json:
        print(json.dumps({"results": results, "errors": errors}, indent=2, ensure_ascii=False))
    else:
        print("\n\n".join(report(r) for r in results))
        for e in errors:
            print("error: " + e)
        print("Numbers only, from the filings. Not financial advice.")
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
