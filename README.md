# The Day Trader skill, B3 edition

11 Claude skills for day trading on B3 (Bovespa): scan every Brazilian chart, track CVM filings and breaking news, time every entry and exit, and replay every outcome before you risk a real.

> **This is a clone of [Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
> (MIT), adapted to the Brazilian market.** The original pack trades US
> stocks (NYSE / Nasdaq, SEC EDGAR, Alpaca). This fork trades B3 only:
> Sao Paulo time and B3's session and holidays, B3 tickers (PETR4, VALE3,
> PETR4F), standard lots of 100 and the fractional market, reais, CVM
> instead of the SEC, and no automatic orders at all. Credit for the
> original design, the algorithms and the Time Machine goes to Jake
> Schincariol. See [What changed from the original](#what-changed-from-the-original).

Free, MIT, no signup, no API key. The tools are plain Python that read free
public data: Yahoo Finance charts for `.SA` tickers, CVM's open data
(dados.cvm.gov.br) and CVM's RAD filing system, and Google News in
Portuguese.

**Nothing in this pack can place an order.** You type every order into your
own broker. A Claude Code hook (the order guard) blocks every order tool and
makes the browser read-only on broker pages. When you allow it, the skills
read real-time prices from your XP account in your browser, read-only, and
never click or type there.

## Install

As a plugin, in Claude Code (this also installs the order guard hook):

```
/plugin marketplace add dvriesman/day-trader-skill
/plugin install day-trader-skill@day-trader-skill
```

Claude Code namespaces plugin skills, so they show up as
`/day-trader-skill:trader-scanner` and so on.

Or work inside this repository: `.claude/settings.json` wires the same order
guard for every session started here.

```bash
git clone https://github.com/dvriesman/day-trader-skill.git
cd day-trader-skill && claude
```

Copying only the `trader-*` folders into `~/.claude/skills/` works too, but
leaves the order guard out: copy `hooks/order_guard.py` and the `hooks` block
of `.claude/settings.json` into your own settings if you go that way.

The tools need Python 3.8 or newer. Nothing to pip install. No keys, no
user-agent, no email: CVM's open data and RAD are public.

## The eleven

| command | job | what it does |
| --- | --- | --- |
| `/trader-scanner` | Chart Scanner | Scans every B3 share for setups: opening-range breakouts, flags, oversold bounces. |
| `/trader-news` | News Monitor | Fatos relevantes and comunicados from CVM the minute they are filed, plus headlines and the day's move. |
| `/trader-entry` | Entry Timer | Watches a setup bar by bar and tells you the moment to get in. |
| `/trader-exit` | Exit Planner | Sets your stop (trigger and limit) and your target before you enter. |
| `/trader-timemachine` | Time Machine | Replays the trade 10,000 times from the stock's own bars, plus every past day the setup fired. |
| `/trader-risk` | Risk Manager | Checks every order against your limits, B3's lots and the session window. |
| `/trader-budget` | Budget Manager | Sizes every position to your budget and max loss, in lots of 100 or the fractional market. |
| `/trader-broker` | Order Ticket | Shows the ticket the way your platform asks for it, records your yes; you type the order. |
| `/trader-alerts` | Price Alerts | The B3 clock, quotes, live XP prices, and alerts on a price, a stop or the flat-by time. |
| `/trader-analyst` | Earnings Analyst | Quarterly results from CVM's ITR and DFP: revenue, EBIT, net income, margins, EPS. |
| `/trader-insiders` | Insider Tracker | What controllers, boards and officers bought and sold, from CVM's monthly reports. |

## How to use it

Everything lands in a `trader/` folder in your project, and each skill reads
what the last one wrote:

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
alerts: stop, target, flat by 11:45                                            (while it is open)
news / analyst / insiders                                                      (context)
```

First time: copy `trader-budget/config.example.json` to `trader/config.json`
and set your budget and limits in reais. The defaults trade the morning:
entries from 10:15 to 11:30, flat by 11:45.

An example, real data: PETR4, Thursday November 12 2026.

1. **`/trader-scanner`** at 10:30: of 62 usable charts, 20 had closed above
   their 15-minute opening range. PETR4 is one: range 43.23 to 44.09, the
   10:20 bar closed at 44.10.
2. **`/trader-news`**: Petrobras's fatos relevantes of November 10 (updated
   2026 projections, shareholder remuneration), its 3Q26 results release, and
   the comunicado on the Logum deal of November 12, straight from CVM.
3. **`/trader-entry`** watched PETR4 bar by bar: at 10:22 waiting (the 10:20
   bar still forming), at 10:25 triggered at 44.10.
4. **`/trader-exit`**: stop at the range midpoint, 43.66 (0.44 a share at
   risk), target 1R, 44.54, flat by 11:45.
5. **`/trader-timemachine`**: 10,000 replays to 11:45 from PETR4's own bars
   over 53 sessions: target first 35.7 %, stop first 29.4 %, neither 34.9 %.
   The same rule on those 53 real days fired 32 times: 18 target, 9 stop, 5
   neither. Verdict: close to a coin flip.
6. **`/trader-budget`**, budget R$ 20.000, max loss R$ 100: 214 shares fit,
   rounded down to the standard lot, 200 PETR4 (R$ 8.820). Max loss at the
   stop R$ 93,27 including fees. Stop-limit: trigger 43.66, limit 43.44.
7. **`/trader-risk`**: PASS or BLOCK, with the reason.
8. **`/trader-broker`** shows the ticket ("compra 200 PETR4 a mercado; OCO:
   disparo 43,66, limite 43,44, alvo 44,54"). You reply "sim" for that one
   order and type it into XP yourself.
9. **`/trader-alerts`** rings on the stop, the target and at 11:45.

## Live prices from XP (you decide, every session)

Free Yahoo data for B3 is about 15 minutes late. When a skill needs a live
price it asks first: "Can I read real-time prices from your XP account in
your browser?" If you say yes, Claude (with Claude in Chrome) reads the
quote off the XP page where you are logged in, records it with
`alerts.py set-price ... --source xp-browser`, and says which numbers are
live and which are delayed. You log in yourself; Claude never asks for,
types or reads back a password or a code, and never clicks or types on the
broker page: the order guard blocks it.

## The order guard (no automatic orders)

`hooks/order_guard.py` is a Claude Code hook that runs before every MCP tool
and every shell command:

- it **denies every order tool** (anything that places, changes, cancels or
  closes an order or a position, from any broker connector);
- once a broker page (XP, Rico, Clear, BTG, NuInvest, Inter, Toro, Genial ...)
  is open in the session, it makes the **browser read-only**: no clicks,
  typing, forms, scripts or uploads; reading, scrolling and screenshots
  still work;
- it **denies shell commands that call a broker** over the network.

It has no off switch. It is wired by `hooks/hooks.json` (the plugin) and by
`.claude/settings.json` (this repo). See [SECURITY.md](SECURITY.md).

## The tools

```bash
python3 trader-scanner/scan.py universe                                # CVM's list of B3 tickers
python3 trader-scanner/scan.py daily                                   # pre-market setups
python3 trader-scanner/scan.py orb --at 10:30                          # opening-range breakouts so far
python3 trader-news/news.py --tickers PETR4 --hours 24                 # CVM filings, headlines, the move
python3 trader-entry/entry.py check PETR4 --setup orb                  # waiting / triggered / expired
python3 trader-exit/exit.py plan --stop or-mid --target 1R --write     # -> trader/plan.json
python3 trader-timemachine/timemachine.py run --write                  # 10,000 replays + the history
python3 trader-budget/size.py --write                                  # -> trader/orders.json
python3 trader-risk/check.py                                           # PASS / BLOCK per order
python3 trader-broker/ticket.py show                                   # the tickets, READY or not
python3 trader-alerts/alerts.py clock                                  # is B3 open?
python3 trader-alerts/alerts.py set-price PETR4 44.12                  # a live price you read on XP
python3 trader-analyst/facts.py PETR4                                  # quarters, growth, margins
python3 trader-insiders/insiders.py company PETR4                      # insider trades by group
```

```bash
python3 -m unittest discover -s tests -v
```

The tests run offline, on fixtures cut from real data: PETR4's 5-minute bars
for the 60 days to November 13 2026, a 5-day bundle of 67 liquid B3 charts,
CVM's FCA, ITR, DFP and VLMO files (cut down to a few companies), RAD's
filing list and Google News in Portuguese. They reproduce the numbers above.

## What changed from the original

| | original (US) | this fork (B3) |
| --- | --- | --- |
| clock | New York, NYSE holidays, 1 p.m. early closes | Sao Paulo (UTC-3), B3 holidays, Ash Wednesday 13:00 open, session to 17:55 or 16:55 with US daylight time |
| tickers | TSLA, BRK.B | PETR4, TAEE11, B3SA3, PETR4F (fractional); `.SA` on Yahoo |
| lots | any whole share | lots of 100, or 1 to 99 on the fractional market |
| money | dollars, `_usd` config keys | reais, `_brl` keys, Brazilian formatting, B3 fees in the max loss |
| stops | broker bracket | stop-limit (trigger and limit) in an OCO, typed by you |
| universe | Nasdaq Trader lists | CVM's FCA (every share and unit trading on B3) |
| news | Yahoo RSS + SEC 8-Ks | CVM RAD (fatos relevantes, comunicados) + Google News pt-BR |
| earnings | SEC XBRL | CVM ITR / DFP |
| Pelosi tracker | House Clerk PTRs | Insider tracker: CVM's monthly insider reports (RCVM 44) |
| orders | Alpaca / IBKR connector, Allow once card | none: you type every order; an order guard hook blocks order tools and broker-page clicks |
| live data | Alpaca quotes | asks to read XP in your browser, read-only |
| PDT rule | FINRA pattern day trader | removed (no such rule on B3); Brazilian day-trade tax noted |

The security review that came with the adaptation is in [SECURITY.md](SECURITY.md).

## Fine print

**Not financial advice.** This is software that reads public data and does
arithmetic. It does not know your situation and it cannot see the future.
Most people who day trade lose money. You can lose money. Decide for
yourself, and talk to a licensed professional if you need advice.

**You place every order.** Nothing here sends one. `/trader-broker` only
shows a ticket after `/trader-risk` passed that exact order on the real
clock in the last 15 minutes and you typed an explicit yes for it, and then
you enter it in your broker.

**Paper first.** `"paper": true` is the default. Practise on your broker's
simulator before real money.

**The data is free, unofficial and late.** Yahoo's B3 data is about 15
minutes behind and its 5-minute bars stop at 17:00 even when B3 trades to
17:55. CVM's open-data files refresh about weekly (RAD is live). Check
anything that matters on your broker's screen before you act.

**The Time Machine is a simulation, not a prediction.** It is built from
the stock's own last 60 days of 5-minute bars.

**A stop is a plan, not a guarantee.** A B3 stop is a stop-limit: in a fast
fall the price can jump past its limit and it does not fill at all.

**Taxes.** Day-trade gains are taxed at 20 % (DARF 6015, due by the last
business day of the following month) with 1 % withheld at source, losses
offset only day-trade gains, and there is no R$ 20.000 monthly exemption for
day trades. Nothing here is tax advice.

**B3's calendar changes.** The 2026 holidays are B3's; the 2027 list is
provisional until B3 publishes it, and B3 can move its trading hours by
circular. Override them with `session_open` / `session_close`.

## Files

```
trader-scanner/     SKILL.md, scan.py, b3.py
trader-news/        SKILL.md, news.py, b3.py
trader-entry/       SKILL.md, entry.py, b3.py
trader-exit/        SKILL.md, exit.py, b3.py
trader-timemachine/ SKILL.md, timemachine.py, b3.py
trader-risk/        SKILL.md, check.py, b3.py
trader-budget/      SKILL.md, size.py, b3.py, config.example.json
trader-broker/      SKILL.md, ticket.py, b3.py
trader-alerts/      SKILL.md, alerts.py, b3.py, alerts.example.json
trader-analyst/     SKILL.md, facts.py, b3.py, metrics.example.json
trader-insiders/    SKILL.md, insiders.py, b3.py
hooks/              order_guard.py, hooks.json (the order guard)
.claude/            settings.json (the order guard for this repo)
tests/              unittest suite + offline fixtures
```

`b3.py` is the same file in every folder (the B3 clock, calendar, tickers and
money), so each skill still works on its own; the tests fail if the copies
drift apart.

MIT. Original by [Jake Schincariol](https://opusjake.ai)
([Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)).
B3 adaptation by [dvriesman](https://github.com/dvriesman).
