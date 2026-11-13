---
name: trader-timemachine
description: >-
  Time Machine for B3. Replays a planned day trade before you risk a real:
  10,000 simulated runs of the rest of the trade, each 5-minute bar drawn
  from the stock's own bars at the same time of day over its last 60 days,
  plus the same rule re-run on every one of those real sessions. Says how
  often the target came first, the stop came first, or neither by the flat-by
  time, the spread of prices, the average R, and a plain verdict (coin flip,
  leans, strongly). Use when the user says "what are the odds", "quais as
  chances", "replay this trade", "backtest this setup", "would this have
  worked", "simulate it", or after /trader-exit and before /trader-budget.
argument-hint: "[run|chart] [--until HH:MM] [--seed N]"
---

# trader-timemachine

Before you risk a real, look at what this exact plan would have done on this
stock's recent days: 10,000 replays built from its own 5-minute bars, and the
same rule on every real session in the data. It is a reality check, not a
prediction: a fato relevante, a results release, a Copom decision or an
auction can do what the last 60 days never did.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                 # trader/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write         # also trader/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 16:55   # replay to the end of the data's session
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from        # ASCII fan chart
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol PETR4 --date 2026-11-12 --entry 44.10 \
    --stop 43.66 --target 44.54 --entry-time 10:25
```

## What it reads and writes

| file / source | what |
| --- | --- |
| `trader/plan.json` | entry, stop, target, entry time, opening range, flat_by |
| Yahoo v8 chart | `PETR4.SA`, 60 days of 5-minute bars, one request |
| `trader/timemachine.json` | the result, with the fan-chart bands |

## How it handles B3

- The session grid starts at 10:00; its length is what the data shows for
  most sessions (Yahoo's B3 bars run 10:00 to about 16:55).
- A missing 10:00 auction bar is rebuilt flat at the next bar's open; any
  other gap or a late open (Ash Wednesday) leaves the session out, and the
  report lists them.
- By default each replay stops at the plan's flat_by (11:45).

## Steps

1. After `/trader-exit`: `timemachine.py run --write`.
2. Report target first, stop first, neither, the price range at flat-by, the
   history line and the verdict, in that order. Mention the seed.
3. Never present it as a probability of profit: it is the stock's own recent
   behaviour, replayed.
4. Next: `/trader-budget`.

## Example: PETR4, 2026-11-12, 10:25 entry, 1R

```
  target first   35.7 %
  stop first     29.4 %
  neither        34.9 %   still open at 11:45
History: the same rule ... on each of those 53 real sessions fired 32 times: 18 target, 9 stop, 5 neither.
Close to a coin flip: target first 35.7 % vs stop first 29.4 %.
```

## What it never does

It never says a trade will work, never changes the plan, never sizes or
places anything.

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
