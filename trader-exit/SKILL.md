---
name: trader-exit
description: >-
  Exit Planner for B3. Sets the stop and the target BEFORE you enter, so you
  know when to get out: stop at the opening-range midpoint or low, a price, a
  percent, or K x ATR; target at 1R, 2R or a price; and a flat-by time
  (11:45 Sao Paulo by default) to be out the same day. Rounds a long's stop
  down to the cent, refuses a stop inside two ticks or 0.05 % of the price,
  and writes trader/plan.json for /trader-timemachine, /trader-budget and
  /trader-alerts. Long side only. Use when the user says "onde coloco o
  stop", "where do I put my stop", "set a target", "plan the exit", "how much
  am I risking", "what is my R", or right after /trader-entry triggers.
argument-hint: "[--stop or-mid|or-low|price:X|pct:X|atr:K] [--target 1R|2R|price:X]"
---

# trader-exit

The Exit Planner writes down the way out before the way in. A stop decided
after the entry is decided by fear; a stop decided before it is a plan.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan                                   # from trader/entry.json: or-mid, 2R
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop or-mid --target 1R --write
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol PETR4 --entry 44.10 --or-high 44.09 --or-low 43.23
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop atr:1.5 --offline PETR4.json
python3 "${CLAUDE_SKILL_DIR}/exit.py" show
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/entry.json` | the trigger from `/trader-entry` |
| `trader/config.json` | `flat_by` (11:45), `min_reward_risk` (1.0, a warning here) |
| Yahoo bars | only for an `atr:K` stop |
| `trader/plan.json` | entry, stop, target, risk a share, reward:risk, flat_by, in reais |

## The rules

- A long's stop rounds DOWN to the cent; a target rounds half up.
- Refused: a stop at or above the entry, a target at or below it, a risk
  under 2 ticks (R$ 0,02) or under 0.05 % of the price.
- B3 stops are stop-limit orders: a trigger ("disparo") and a limit
  ("limite"). The plan's stop is the trigger; `/trader-budget` adds the limit
  and the ticket shows both. In a fast fall a stop-limit can be skipped over.
- Flat-by: a time at or after the end of continuous trading (16:55 / 17:55)
  moves up to 10 minutes before it: the closing call is an auction.
- If the entry came from a live XP price rather than the delayed trigger
  (see /trader-entry), plan from that price: `--entry 44.18`.

## Steps

1. After a trigger: `exit.py plan --write` (or with the user's stop and target).
2. Read back entry, stop, target, risk a share and R in one short block.
3. Next: `/trader-timemachine` for the odds, `/trader-budget` for the size.

## Example: PETR4, 2026-11-12

```
Exit plan: PETR4 long, Thu 2026-11-12, opening-range breakout on 5-minute bars
  Entry         R$ 44,10 at 10:25 (bar 4)
  Stop          R$ 43,66  opening-range midpoint: (R$ 44,09 + R$ 43,23) / 2 = 43.66
  Target        R$ 44,54  1R (one times the risk)
  Risk a share  R$ 0,44 (1.00% of the entry)
  Flat by       11:45 Sao Paulo, win or lose: no overnight hold
```

## What it never does

It never moves a stop further away after the entry, never places the stop
order (the user does, in their broker), and never plans a short.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- **Nothing in this pack places an order, and nothing can.** On B3 the user types every order into
  their own broker (XP, the home broker, Profit, MetaTrader 5) after three gates: trader-risk passed that
  exact order on the real clock in the last 15 minutes, the user typed an explicit yes ("sim" / "yes")
  for that one order in the chat, and the user enters it themselves. The order guard hook
  (`hooks/order_guard.py`) denies every order tool and every click or keystroke in the browser once a
  broker page is open.
- Paper / simulator by default (`"paper": true`). Going live is the user's own decision, in their own config.
- No options, margin, day-trade leverage or short selling unless the config allows it and the user confirms.
- Never ask for, read, store or repeat a password, a token, an account number or a 2FA code.
- Day-trade gains in Brazil are taxed at 20 % (DARF code 6015, by the last business day of the next
  month) with 1 % withheld at source ("dedo-duro"); there is no R$ 20.000 exemption for day trades. Not
  tax advice: talk to an accountant.
