---
name: backtest
description: Run backtests to validate strategy parameter changes; required for any strategy modification.
---

# Backtest

## When Required

- Any change to strategy parameters (weights, thresholds, indicators)
- Any change to signal combiner logic
- Any change to SL/TP/trailing stop parameters
- Any change to market state detection

## Commands

```bash
# Full 540-day futures portfolio backtest (standard validation)
cd backend && .venv/bin/python backtest.py \
  --futures --portfolio --leverage 3 \
  --trade-cooldown 6 --min-sell-weight 0.20 \
  --dynamic-sl --short-all --days 540

# Spot backtest
cd backend && .venv/bin/python backtest.py \
  --spot --portfolio --days 540

# Single strategy test
cd backend && .venv/bin/python backtest.py \
  --futures --strategy bollinger_rsi --days 540
```

## Validation Criteria

- Total return must be positive over 540 days
- Max drawdown must be within risk parameters
- Sharpe ratio should not decrease vs current parameters
- Win rate should be reasonable (>40%)

## Rules

- ALWAYS run BEFORE deploying strategy changes
- Record full results in workpad Notes section
- If backtest shows regression, REVERT the parameter change
- Compare new results with existing documented results in PROGRESS.md
- Do NOT deploy strategy changes without passing backtest validation
