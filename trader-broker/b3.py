"""B3 (Bovespa) clock, calendar, symbols and money. Standard library only.

One identical copy of this file lives in every skill folder that has a Python
tool, so each folder still works on its own when copied into ~/.claude/skills.
tests/test_repo.py fails if the copies drift apart: edit one, copy it to all.

Time. Brazil has had no daylight saving time since 2019, so Sao Paulo
(America/Sao_Paulo, BRT) is UTC-3 all year. Every tool works in naive
datetimes: naive UTC internally, naive Sao Paulo wall clock for people.

The B3 session (cash equities, "mercado a vista", and the fractional market).
B3 moves its grid with US daylight saving time:

    US standard time (early Nov to mid Mar)   US daylight time (mid Mar to early Nov)
      pre-opening    09:45 - 10:00              09:45 - 10:00
      trading        10:00 - 17:55              10:00 - 16:55
      closing call   17:55 - 18:00              16:55 - 17:00

Source: b3.com.br, "Horario de negociacao, acoes", read 2026-11-13, and B3's
Oficio Circular 055/2026-VNC for the 2026-11-03 change. B3 announces every
change by circular: when it moves the grid, set "session_open" and
"session_close" in trader/config.json, or edit SESSION below.

session(d) returns (open, close) where close is the END OF CONTINUOUS TRADING
(17:55 or 16:55): the closing call after it is an auction, not a time to get
in or out of a day trade at a known price.

Holidays: B3's 2026 trading calendar (Oficio Circular 003/2026-VNC) and the
2027 dates that follow from the national holidays and Easter. 2027 is
PROVISIONAL until B3 publishes its 2027 calendar. B3 trades on Sao Paulo's
local holidays (25 Jan, 9 Jul) since 2022.
"""

import datetime
import re

BRT = datetime.timedelta(hours=-3)
EPOCH = datetime.datetime(1970, 1, 1)
TZ_LABEL = "BRT"
PLACE = "Sao Paulo"

HOLIDAYS = {
    "2026-01-01": "Confraternizacao Universal (New Year's Day)",
    "2026-02-16": "Carnaval (Monday)",
    "2026-02-17": "Carnaval (Tuesday)",
    "2026-04-03": "Sexta-feira Santa (Good Friday)",
    "2026-04-21": "Tiradentes",
    "2026-05-01": "Dia do Trabalho (Labour Day)",
    "2026-06-04": "Corpus Christi",
    "2026-09-07": "Independencia do Brasil",
    "2026-10-12": "Nossa Senhora Aparecida",
    "2026-11-02": "Finados",
    "2026-11-20": "Dia da Consciencia Negra",
    "2026-12-24": "Vespera de Natal (no trading session)",
    "2026-12-25": "Natal (Christmas)",
    "2026-12-31": "Vespera de Ano Novo (no trading session)",
    # 2027: provisional until B3 publishes its calendar
    "2027-01-01": "Confraternizacao Universal (New Year's Day)",
    "2027-02-08": "Carnaval (Monday)",
    "2027-02-09": "Carnaval (Tuesday)",
    "2027-03-26": "Sexta-feira Santa (Good Friday)",
    "2027-04-21": "Tiradentes",
    "2027-05-27": "Corpus Christi",
    "2027-09-07": "Independencia do Brasil",
    "2027-10-12": "Nossa Senhora Aparecida",
    "2027-11-02": "Finados",
    "2027-11-15": "Proclamacao da Republica",
    "2027-12-24": "Vespera de Natal (no trading session)",
    "2027-12-31": "Vespera de Ano Novo (no trading session)",
}
LATE_OPEN = {  # Ash Wednesday: pre-opening 12:45, trading from 13:00
    "2026-02-18": (datetime.time(13, 0), "Quarta-feira de Cinzas (Ash Wednesday)"),
    "2027-02-10": (datetime.time(13, 0), "Quarta-feira de Cinzas (Ash Wednesday)"),
}
CALENDAR_YEARS = (2026, 2027)
PROVISIONAL_YEARS = (2027,)
SESSION = {
    "open": datetime.time(10, 0),
    "close_us_standard": datetime.time(17, 55),   # then the closing call to 18:00
    "close_us_daylight": datetime.time(16, 55),   # then the closing call to 17:00
}
CALL_MINUTES = 5

LOT = 100              # the standard lot ("lote padrao"); 1 to 99 shares trade as <TICKER>F
TICK = 0.01            # minimum price step for stocks, BDRs and ETFs (R$ 0,01)
INDEX = "^BVSP"        # the Ibovespa, on Yahoo

# B3 cash-market tickers: a 4-character root (letters, B3SA has a digit) + 1 or 2 digits
# (PETR4, VALE3, TAEE11, B3SA3, BOVA11, AAPL34 for a BDR), with an F for the fractional market (PETR4F).
TICKER = re.compile(r"^[A-Z][A-Z0-9]{3}[0-9]{1,2}F?$")
HHMM = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$")


# ---------------------------------------------------------------- clock

def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def to_brt(utc):
    """naive UTC -> naive Sao Paulo wall clock."""
    return utc.replace(tzinfo=None) + BRT


def brt_to_utc(local):
    """naive Sao Paulo wall clock -> naive UTC."""
    return local.replace(tzinfo=None) - BRT


def utc_of(ts):
    """Unix seconds -> naive UTC, without the machine's time zone."""
    return EPOCH + datetime.timedelta(seconds=int(ts))


def ts_of(utc):
    return int((utc.replace(tzinfo=None) - EPOCH).total_seconds())


def ts_to_brt(ts):
    return to_brt(utc_of(ts))


def today_brt(now_utc=None):
    return to_brt(now_utc or utc_now()).date()


def parse_now(text):
    """--now -> naive UTC. "2026-11-10 10:15" (or ...T10:15, or just 10:15 for
    today) is Sao Paulo time; an offset or Z makes it absolute. ValueError if bad."""
    if not text:
        return utc_now()
    t = str(text).strip()
    for suffix in (" BRT", " SP"):
        if t.upper().endswith(suffix):
            t = t[:-len(suffix)].strip()
    if re.match(r"^\d{1,2}:\d{2}$", t):
        t = "%s %s" % (today_brt().isoformat(), t.zfill(5))
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise ValueError('wants a Sao Paulo time like "2026-11-10 10:15" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return brt_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_utc(text):
    """"2026-11-10T13:15:00Z" -> naive UTC, or None."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def parse_hhmm(text):
    """"10:15" -> time(10, 15); ValueError otherwise."""
    m = HHMM.match(str(text if text is not None else ""))
    if not m:
        raise ValueError("wants a Sao Paulo time like 10:15, not %r" % (text,))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def hm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def iso_brt(now_utc):
    """naive UTC -> "2026-11-10T10:15:00-03:00"."""
    return to_brt(now_utc).replace(microsecond=0).isoformat() + "-03:00"


def iso_utc(now_utc=None):
    return (now_utc or utc_now()).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- calendar

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def us_daylight(d):
    """Is the US on daylight saving time on this date? (second Sunday of March to
    the first Sunday of November, US rule since 2007). B3 shortens its session then."""
    return _nth_sunday(d.year, 3, 2) <= d < _nth_sunday(d.year, 11, 1)


def trading_day(d):
    """(is_trading_day, reason). Weekends and the B3 holiday list."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = HOLIDAYS.get(d.isoformat())
    if name:
        return False, "B3 holiday: " + name
    return True, ""


def calendar_note(d):
    """A warning when the date is outside the known (or confirmed) calendar, else ''."""
    if d.year not in CALENDAR_YEARS:
        return "the B3 holiday list covers %s only: check b3.com.br for %s" % (
            " and ".join(map(str, CALENDAR_YEARS)), d.isoformat())
    if d.year in PROVISIONAL_YEARS:
        return "the %d holiday list is provisional until B3 publishes its calendar: check b3.com.br" % d.year
    return ""


def _cfg_time(cfg, key):
    v = (cfg or {}).get(key)
    if v in (None, "", False):
        return None
    return parse_hhmm(v)


def session(d, cfg=None):
    """(open, close) Sao Paulo time for the continuous session, or None if closed.
    close is the end of continuous trading; the closing call runs CALL_MINUTES after it.
    "session_open" / "session_close" in the config override the grid."""
    ok, _ = trading_day(d)
    if not ok:
        return None
    o = _cfg_time(cfg, "session_open") or SESSION["open"]
    late = LATE_OPEN.get(d.isoformat())
    if late and late[0] > o:
        o = late[0]
    c = _cfg_time(cfg, "session_close") or (
        SESSION["close_us_daylight"] if us_daylight(d) else SESSION["close_us_standard"])
    return o, c


def call_end(close):
    dt = datetime.datetime.combine(datetime.date(2000, 1, 1), close) + datetime.timedelta(minutes=CALL_MINUTES)
    return dt.time()


def session_note(d):
    """Why today's session is special, or ''."""
    late = LATE_OPEN.get(d.isoformat())
    if late:
        return "late open at %s today (%s)" % (hm(late[0]), late[1])
    return ""


def next_open(d, cfg=None):
    """"2026-11-16 10:00": the next session's open after day d, or None."""
    nd = d + datetime.timedelta(days=1)
    for _ in range(15):
        s = session(nd, cfg)
        if s:
            return "%s %s" % (nd.isoformat(), hm(s[0]))
        nd += datetime.timedelta(days=1)
    return None


def trading_days_back(today, n):
    """The last n B3 trading days up to and including today, newest first."""
    out, d = [], today
    for _ in range(10 * n + 15):
        if trading_day(d)[0]:
            out.append(d.isoformat())
            if len(out) == n:
                break
        d -= datetime.timedelta(days=1)
    return out


# ---------------------------------------------------------------- symbols

def norm(sym):
    """" petr4.sa " -> "PETR4". Raises ValueError for anything that is not a B3
    cash-market ticker, so nothing odd ever reaches a URL, a file name or an order."""
    s = str(sym or "").strip().upper()
    if s.endswith(".SA"):
        s = s[:-3]
    if not TICKER.match(s):
        raise ValueError("%r is not a B3 ticker (like PETR4, VALE3, TAEE11 or PETR4F)" % (sym,))
    return s


def is_ticker(sym):
    try:
        norm(sym)
        return True
    except ValueError:
        return False


def is_fractional(sym):
    return norm(sym).endswith("F")


def base(sym):
    """PETR4F -> PETR4 (the fractional market trades the same share)."""
    s = norm(sym)
    return s[:-1] if s.endswith("F") else s


def fractional(sym):
    """PETR4 -> PETR4F."""
    return base(sym) + "F"


def yahoo(sym):
    """PETR4 / PETR4F -> PETR4.SA; ^BVSP stays."""
    if str(sym).strip().upper() == INDEX:
        return INDEX
    return base(sym) + ".SA"


def from_yahoo(ysym):
    """PETR4.SA -> PETR4 (None for anything else)."""
    s = str(ysym or "").strip().upper()
    if s == INDEX:
        return INDEX
    if not s.endswith(".SA"):
        return None
    return s[:-3] if TICKER.match(s[:-3]) else None


def split_lots(qty, lot=LOT):
    """84 -> (0, 84), 250 -> (200, 50): (standard-lot shares, fractional shares)."""
    q = int(qty)
    return (q // lot) * lot, q % lot


# ---------------------------------------------------------------- money and prices

def _br(x, places=2):
    s = "{:,.{p}f}".format(float(x), p=places)
    return s.replace(",", "_").replace(".", ",").replace("_", ".")


def brl(x, places=2):
    """4518.5 -> "R$ 4.518,50" (Brazilian format, for people)."""
    x = float(x)
    sign = "-" if x < 0 else ""
    return "%sR$ %s" % (sign, _br(abs(x), places))


def px(x):
    """A price for people: 45.18 -> "45,18"."""
    return _br(x, 2)


def pct_br(x, places=2):
    return "%s%%" % _br(x, places)


def parse_number(text):
    """"45,18" / "45.18" / "R$ 1.234,56" / "1,234.56" -> float, or None."""
    t = str(text or "").strip().replace("R$", "").replace(" ", "")
    if not t:
        return None
    if "," in t and "." in t:
        if t.rfind(",") > t.rfind("."):
            t = t.replace(".", "").replace(",", ".")     # 1.234,56
        else:
            t = t.replace(",", "")                       # 1,234.56
    elif "," in t:
        t = t.replace(",", ".")                          # 45,18
    try:
        return float(t)
    except ValueError:
        return None


def round_tick(x):
    """To the cent, half up."""
    return int(float(x) * 100 + 0.5 + 1e-9) / 100.0


# ---------------------------------------------------------------- CVM open data (dados.cvm.gov.br)

CVM_DATA = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/%s/DADOS/%s_cia_aberta_%d.zip"
CVM_MEMBER_MAX = 400 * 1024 * 1024     # the biggest ITR CSV is about 130 MB unpacked


def fold(text):
    """Lowercase without accents: "acoes ordinarias" for "Ações Ordinárias"."""
    import unicodedata
    t = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in t if not unicodedata.combining(ch)).lower().strip()


def cvm_csv_rows(zip_path_or_bytes, pattern):
    """Iterate the rows (dicts) of the CSV in a CVM zip whose name matches the regex
    `pattern`. CVM's CSVs are latin-1 and ';'-separated. Streams, so a big file is fine.
    Raises ValueError for a bad zip, a missing member or one over CVM_MEMBER_MAX."""
    import csv
    import io
    import zipfile
    src = io.BytesIO(zip_path_or_bytes) if isinstance(zip_path_or_bytes, bytes) else zip_path_or_bytes
    try:
        z = zipfile.ZipFile(src)
    except zipfile.BadZipFile:
        raise ValueError("not a zip file")
    with z:
        names = [n for n in z.namelist() if re.search(pattern, n)]
        if not names:
            raise ValueError("no file matching %s in the zip" % pattern)
        info = z.getinfo(names[0])
        if info.file_size > CVM_MEMBER_MAX:
            raise ValueError("%s is %d MB unpacked: refused" % (names[0], info.file_size >> 20))
        with z.open(names[0]) as raw:
            for row in csv.DictReader(io.TextIOWrapper(raw, encoding="latin-1", newline=""), delimiter=";"):
                yield row


def fca_ticker_map(fca_zip):
    """CVM FCA zip (path or bytes) -> {TICKER: {"cvm": "009512", "cnpj": ..., "name": ...}}
    for every share or unit with a trading code on the B3 exchange."""
    codes = {}
    for r in cvm_csv_rows(fca_zip, r"fca_cia_aberta_geral_\d{4}\.csv$"):
        codes[r.get("CNPJ_Companhia")] = ((r.get("Codigo_CVM") or "").strip().zfill(6),
                                          " ".join((r.get("Nome_Empresarial") or "").split()))
    out = {}
    for r in cvm_csv_rows(fca_zip, r"valor_mobiliario_\d{4}\.csv$"):
        t = (r.get("Codigo_Negociacao") or "").strip().upper()
        if not t or not is_ticker(t) or fold(r.get("Mercado")) != "bolsa":
            continue
        code, name = codes.get(r.get("CNPJ_Companhia"), ("", ""))
        if code.strip("0"):
            out[t] = {"cvm": code, "cnpj": r.get("CNPJ_Companhia"), "name": name}
    return out
