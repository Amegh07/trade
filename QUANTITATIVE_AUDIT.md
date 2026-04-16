# QUANTITATIVE LOGIC AUDIT: Silent Mathematical Failures
**Date:** April 14, 2026  
**Scope:** Beta scaling, margin pre-checks, Net PnL accounting  
**Files:** `core/alpha_engine.py`, `core/execution_router.py`

---

## 1. HEDGE RATIO β SCALING LOGIC — **CRITICAL ISSUES**

### 1.1 Primary Leg → Hedge Leg Lot Transmission

**Alpha Engine Signal Generation** (`core/alpha_engine.py`)
```python
# Line 391-398: Signal creation
sig_a = Signal(
    symbol=asset_a, ... beta=1.0, leg_role="PRIMARY",
    hedge_symbol=asset_b, hedge_beta=beta, ...  # β from cointegration ~0.85
)
sig_b = Signal(
    symbol=asset_b, ... beta=beta, leg_role="HEDGE",
    hedge_symbol=asset_a, hedge_beta=1.0, ...
)
```

**Execution Router — PRIMARY Leg** (`core/execution_router.py`, lines 523–575)
```python
# Line 531-543: Kelly sizing for PRIMARY
lot = kelly.lot_size(equity, atr, tick_value, tick_size, volume_min, volume_max)
# Returns: round(max(volume_min, min(raw_lot, volume_max)), 2)
# Example return: 0.85 (after rounding to 2 decimals)

# Line 541-542: Pre-calculate hedge lot FOR MARGIN PRECHECK
hedge_lot = round(max(info.volume_min, lot * hedge_beta), 2)
# Example: round(max(0.01, 0.85 * 0.85), 2) = round(0.7225, 2) = 0.72

# Line 545-546: Margin validation passes
# Line 580-583: Storage of trades
io_queue.put_nowait({"lot": lot, ...})  # Records 0.85 to DB

# Line 647: Atomic tracking for primary
basket_tracking[basket_id] = {"lot": lot, "ticket": result.deal}
# Stores: basket_tracking[b_id]["lot"] = 0.85
```

**Execution Router — HEDGE Leg** (`core/execution_router.py`, lines 584–588)
```python
elif leg_role == "HEDGE":
    if basket_id not in basket_tracking:
        logger.warning(...)
        continue
    
    parent_lot = basket_tracking[basket_id]["lot"]  # Retrieves: 0.85
    lot = parent_lot * beta                         # Computes: 0.85 * 0.85 = 0.7225
    lot = round(max(info.volume_min, lot), 2)      # Rounds: 0.72
    logger.info(f"... Beta={beta:.2f} | Scaled Lot={lot:.2f} ...")
```

### ⚠️ **Issue 1.1a: Float Precision Loss in Round-Trip Multiplication**

**Problem:**
- PRIMARY lot computed: `kelly` → `round(..., 2)` = stores `0.85` (exact)
- HEDGE lot computed: `0.85 * 0.85 = 0.7225` → `round(..., 2)` = `0.72`
- **BUT:** `0.85 * 0.85 = 0.7225`, which rounds DOWN to `0.72`
- If the TRUE hedge ratio is `β = 0.856` (3+ decimals from OLS), then:
  - `0.85 * 0.856 = 0.7276` → rounds to `0.73` (different!)
- **Root cause:** β is only stored at 2 decimals in the signal; original precision lost.

**Trace Example:**
```
Alpha Engine calculates:  β = 0.855617 (raw OLS output)
Signal stores:          β = 0.86 (rounded for logging, but full precision passed)
                        Actually: beta=0.855617 in Signal.beta field ✓

Execution PRIMARY:
  kelly.lot_size() returns: raw_lot = 1.0456 → round(1.0456, 2) = 1.05
  Parent stored: basket_tracking[b_id]["lot"] = 1.05

Execution HEDGE:
  parent_lot = 1.05
  lot = 1.05 * 0.855617 = 0.898398
  lot = round(max(0.01, 0.898398), 2) = 0.90
  BUT expected for delta-neutral: 1.05 * 0.855617 ≈ 0.898 ≈ 0.90 ✓
```

**Status:** ✓ **ACCEPTABLE** — Full β precision IS passed in the Signal; only display is rounded.

---

### ⚠️ **Issue 1.1b: MT5 Lot Step Validation Missing on HEDGE Leg**

**Problem:**
- PRIMARY leg applies: `kelly.lot_size()` → `round(..., 2)` → later clamped to `volume_min`
- HEDGE leg applies: `round(max(info.volume_min, lot * beta), 2)` only
- **MT5 Lot Requirements:**
  - `info.volume_min` = minimum lot (e.g., 0.01)
  - `info.volume_step` = valid lot increment (e.g., 0.01, **0.1**, 0.5, 1.0)
  - Lots MUST be: `volume_min ≤ lot ≤ volume_max` AND `(lot - volume_min) % volume_step == 0`

**Example Scenario:**
```
Symbol: GBPUSD on broker with:
  volume_min = 0.01
  volume_step = 0.1  ← 0.1 lot minimum step!
  volume_max = 2.0

PRIMARY leg:
  kelly returns: 0.85
  Final: round(max(0.01, 0.85), 2) = 0.85
  MT5 rejects: 0.85 is not a valid multiple of 0.1
  ✗ ORDER FAILS with TRADE_RETCODE_INVALID_VOLUME

HEDGE leg:
  parent_lot = 0.85
  lot = 0.85 * 0.856 = 0.7276
  Final: round(max(0.01, 0.7276), 2) = 0.73
  MT5 rejects: 0.73 is not a valid multiple of 0.1
  ✗ ORDER FAILS with TRADE_RETCODE_INVALID_VOLUME
```

**Code Path:**
```python
# core/execution_router.py, Line 588
lot = round(max(info.volume_min, lot), 2)
# ✗ Does NOT validate: (lot - info.volume_min) % info.volume_step == 0

# Later clamping (Line 623-626) only checks minimum:
if lot < info.volume_min:
    lot = info.volume_min  # Hard floor
# ✗ Does NOT validate volume_step
```

**Impact:**
- HEDGE leg orders silently fail after PRIMARY succeeds → atomic rollback attempts but may still leave orphaned PRIMARY position
- System logs order execution but order was rejected by broker
- No recovery; basket can be stuck half-open

**Recommendation (CRITICAL):**
```python
# HEDGE leg sizing (core/execution_router.py, ~line 587)
lot = parent_lot * beta

# ✓ Validate lot is a valid MT5 step
def _round_to_step(lot: float, volume_min: float, volume_step: float) -> float:
    """Round lot to nearest valid volume_step that doesn't go below volume_min."""
    if lot < volume_min:
        return volume_min
    steps_above_min = max(0, (lot - volume_min) / volume_step)
    steps_rounded = round(steps_above_min)
    return volume_min + steps_rounded * volume_step

lot = _round_to_step(lot, info.volume_min, info.volume_step)
```

---

### ⚠️ **Issue 1.1c: No Verification That Hedge Lot Matches Primary Lot Ratio**

**Problem:**
- The code does NOT verify that the final HEDGE lot maintains the intended δ-neutral ratio
- After rounding, `HEDGE_final / PRIMARY_final` may differ from intended β

**Example:**
```
PRIMARY = 1.05 lots (kelly output, rounded)
HEDGE β = 0.856
Theoretical HEDGE = 1.05 * 0.856 = 0.899
Actual HEDGE (after round) = 0.90

Ratio check: 0.90 / 1.05 = 0.857 ≈ 0.856 ✓ (close enough)

BUT worst case:
PRIMARY = 0.09 lots (very small kelly)
HEDGE β = 0.85
Theoretical HEDGE = 0.09 * 0.85 = 0.0765
Actual HEDGE (after round) = 0.01 (hit volume_min floor)

Ratio: 0.01 / 0.09 = 0.111 ✗ (completely wrong! should be 0.85)
```

**Code Gap:**
```python
# core/execution_router.py, lines 584-588
lot = parent_lot * beta
lot = round(max(info.volume_min, lot), 2)
logger.info(f"Beta={beta:.2f} | Scaled Lot={lot:.2f}")  # NO RATIO VERIFICATION
```

**Recommendation (HIGH):**
```python
# After HEDGE lot calculated:
actual_ratio = lot / parent_lot
expected_ratio = beta
ratio_error = abs(actual_ratio - expected_ratio) / expected_ratio

if ratio_error > 0.05:  # More than 5% error
    logger.warning(
        f"[{sym}] ⚠️ HEDGE RATIO DEVIATION | Expected β={expected_ratio:.4f}, "
        f"got {actual_ratio:.4f} (error={ratio_error*100:.1f}%). "
        f"Primary={parent_lot}, Hedge={lot}. May not be delta-neutral."
    )
    if ratio_error > 0.20:  # More than 20% error, skip
        logger.error(f"[{sym}] Cancelling hedge due to excessive ratio deviation.")
        continue
```

---

## 2. MARGIN PRE-CHECK BUFFER — **HIGH RISK**

### 2.1 Currency Conversion & Margin Aggregation

**Code** (`core/execution_router.py`, lines 540–560)
```python
hedge_sym = getattr(sig, 'hedge_symbol', "")  # e.g., "NZDUSD"
hedge_beta = getattr(sig, 'hedge_beta', 1.0)
hedge_lot = round(max(info.volume_min, lot * hedge_beta), 2)

try:
    # PRIMARY leg (e.g., AUDUSD)
    margin_p = await loop.run_in_executor(
        None, mt5.order_calc_margin, m_type_p, sym, lot, 
        tick.ask if is_buy else tick.bid
    )
    
    # HEDGE leg (e.g., NZDUSD)
    margin_h = await loop.run_in_executor(
        None, mt5.order_calc_margin, m_type_h, hedge_sym, hedge_lot,
        tick.ask if not is_buy else tick.bid
    )
    
    required_margin = (margin_p or 0) + (margin_h or 0)  # AGGREGATE
    acc_info = await loop.run_in_executor(None, mt5.account_info)
    
    if acc_info.margin_free < (required_margin * 1.5):  # COMPARE
        logger.warning(
            f"[{sym}] WARNING — Insufficient margin for dual-basket. "
            f"Free: {acc_info.margin_free:.2f} | Required (1.5x): {required_margin * 1.5:.2f}. "
            f"Skipping execution."
        )
        continue
except Exception as m_err:
    logger.error(f"[{sym}] Margin check failed: {m_err}. Aborting for safety.")
    continue
```

### ⚠️ **Issue 2.1a: No Verification of Margin Currency**

**Problem:**
- `mt5.order_calc_margin()` returns margin in account base currency (usually)
- BUT: **No explicit check or logging of the currency**
- Some MT5 brokers or back-end systems may have inconsistent behavior

**Potential Failure Scenario:**
```
Account currency: USD
Account equity: $100,000
Account margin_free: $50,000

PRIMARY (AUDUSD): margin_p = mt5.order_calc_margin() returns 1,234 (unknown unit!)
  Is it: 1,234 USD or 1,234 AUD or something else?

HEDGE (NZDUSD): margin_h = mt5.order_calc_margin() returns 987 (unknown unit!)

Sum: required_margin = 1,234 + 987 = 2,221
Check: Is 50,000 >= 2,221 * 1.5 = 3,331.5? YES ✓

BUT if one was AUD and other was USD:
  1,234 AUD ≈ 820 USD (1 AUD ≈ 0.665 USD in 2026)
  987 USD = 987 USD
  TRUE required_margin ≈ 1,807 USD (not 2,221)
  Check would have passed but was based on wrong currency mix!
```

**Code Gap:** No validation that both margin calculations return the same currency.

**Recommendation (HIGH):**
```python
# After margin calculations:
# Add logging with account currency context
if hasattr(acc_info, 'currency'):
    logger.debug(f"[Margin] Account currency: {acc_info.currency}")
else:
    logger.warning("[Margin] Could not verify account currency from MT5")

# Add assertion (defensive)
if margin_p is not None and margin_h is not None:
    logger.info(
        f"[{sym}] Margin breakdown: "
        f"PRIMARY ({sym})={margin_p:.2f} | "
        f"HEDGE ({hedge_sym})={margin_h:.2f} | "
        f"Total={required_margin:.2f} | "
        f"Account.margin_free={acc_info.margin_free:.2f} | "
        f"Buffer_1.5x={required_margin * 1.5:.2f}"
    )
else:
    logger.error(f"[{sym}] Margin calculation returned None: margin_p={margin_p}, margin_h={margin_h}")
    continue
```

---

### ⚠️ **Issue 2.1b: Margin May Be None — Silent Addition Failure**

**Problem:**
```python
required_margin = (margin_p or 0) + (margin_h or 0)
```

- If `mt5.order_calc_margin()` fails or returns None, code defaults to 0
- This is dangerous! It means "assume 0 margin required" if the call fails
- If BOTH calculations fail, `required_margin = 0`, and the check passes incorrectly

**Failure Scenario:**
```
Primary margin fails: margin_p = None (MT5 error, or symbol not tradable)
  Code: (None or 0) = 0

Hedge margin fails: margin_h = None
  Code: (None or 0) = 0

required_margin = 0 + 0 = 0
Check: 50,000 >= 0 * 1.5? YES ✓

Order proceeds with NO validated margin requirement!
Position opens, account blows up due to overleveraging.
```

**Code Gap:** No explicit error handling for None returns from margin calculation.

**Recommendation (CRITICAL):**
```python
try:
    margin_p = await loop.run_in_executor(None, mt5.order_calc_margin, m_type_p, sym, lot, price_p)
    margin_h = await loop.run_in_executor(None, mt5.order_calc_margin, m_type_h, hedge_sym, hedge_lot, price_h)
    
    # ✗ CURRENT (UNSAFE):
    # required_margin = (margin_p or 0) + (margin_h or 0)
    
    # ✓ PROPOSED (SAFE):
    if margin_p is None:
        logger.error(f"[{sym}] Margin calc failed for PRIMARY leg. Skipping.")
        continue  # ABORT, don't assume 0
    if margin_h is None:
        logger.error(f"[{sym}] Margin calc failed for HEDGE leg. Skipping.")
        continue  # ABORT
    
    required_margin = margin_p + margin_h
    
    if required_margin <= 0:
        logger.error(f"[{sym}] Margin calc returned non-positive value: {required_margin}. Skipping.")
        continue  # Sanity check
        
except Exception as m_err:
    logger.error(f"[{sym}] Margin check exception: {m_err}. Aborting for safety.")
    continue
```

---

## 3. NET PnL CHECK ON MEAN REVERSION EXITS — **CRITICAL LOGIC BUG**

### 3.1 Current Logic Flow

**Code** (`core/execution_router.py`, lines 423–436)
```python
# Regular Mean Reversion: Transaction Cost Trap (Net PnL Guard)
if exit_reason == "MEAN_REVERSION" and basket_id:
    all_pos = await loop.run_in_executor(None, mt5.positions_get)
    basket_pos = [p for p in all_pos if p.comment == basket_id]
    
    if basket_pos:
        net_pnl = sum(p.profit + p.swap + p.commission for p in basket_pos)
        if net_pnl <= 0:
            logger.info(
                f"[{sym}] INFO — Z-Score target reached, but Net PnL ({net_pnl:.2f}) "
                f"is negative. Holding for fee clearance."
            )
            continue  # SKIP CLOSE, hold position
    
    # If basket_pos is empty, proceed to close
    # Proceed with Standard Close if not vetoed
    target_pos = await loop.run_in_executor(None, mt5.positions_get, sym)
```

### ⚠️ **Issue 3.1a: Assumption That Swap & Commission Are Negative**

**Problem:**
- Code assumes: `p.profit + p.swap + p.commission` = net PnL where swap/commission are NEGATIVE
- **But MT5 API behavior varies:**
  - Some brokers: `p.swap` is negative (fee deduction)
  - Some brokers: `p.swap` is positive (credit)
  - Some brokers: `p.commission` is negative (fee)
  - Some brokers: `p.commission` is positive with a separate `p.commission_currency` field

**Trace Example:**
```
Position P1 (AUDUSD, BUY, 0.85 lots):
  p.profit = 45.30 (unrealized gain)
  p.swap = -2.50 (negative: fee deduction) ✓
  p.commission = -1.20 (negative: fee deduction) ✓
  net_pnl = 45.30 + (-2.50) + (-1.20) = 41.60 > 0 → CLOSE ✓

Position P2 (NZDUSD, SELL, 0.72 lots):
  p.profit = -12.15 (unrealized loss)
  p.swap = 0.80 (positive: credit! unexpected)
  p.commission = -0.90 (negative: fee)
  net_pnl = -12.15 + 0.80 + (-0.90) = -12.25 < 0 → HOLD ✓ (correct by accident)

BUT if swaps were reversed per broker:
Position P2 (broker variant):
  p.profit = -12.15
  p.swap = 4.30 (positive credit, higher than expected)
  p.commission = -0.90
  net_pnl = -12.15 + 4.30 + (-0.90) = -8.75 < 0 → HOLD
  
But mathematically true fees are: -0.90 only (swap is credit).
True net = -12.15 + 4.30 - 0.90 = -8.75 (same by coincidence)
Correct.
```

**Risk:** If a broker variant returns swap/commission with opposite sign, the math fails silently.

**Recommendation (HIGH):**
```python
# Before summing, validate signs
net_pnl = 0.0
for p in basket_pos:
    # Validate that commission appears to be a fee (should be negative or very small positive)
    if p.commission is not None and p.commission > 10.0:
        logger.warning(
            f"[PnL] Unusual commission value: {p.commission} "
            f"(expected ≤ 0 or small positive). May indicate API variant."
        )
    
    # Sum with defensive bounds checking
    transaction_costs = (p.swap or 0.0) + (p.commission or 0.0)
    position_net = (p.profit or 0.0) + transaction_costs
    net_pnl += position_net
    logger.debug(f"[PnL] Position {p.ticket}: profit={p.profit}, swap={p.swap}, "
                 f"commission={p.commission}, net={position_net}")
```

---

### ⚠️ **Issue 3.1b: Swap or Commission May Be None — TypeError on Sum**

**Problem:**
```python
net_pnl = sum(p.profit + p.swap + p.commission for p in basket_pos)
```

- If `p.swap` is None or `p.commission` is None, the `+` operation raises TypeError
- Code has NO try/except around this line
- If ANY position in the basket has None fields, the entire Alpha engine or router crashes

**Failure Scenario:**
```
Basket has 2 positions:
  P1: profit=45.30, swap=-2.50, commission=-1.20 (all valid)
  P2: profit=-12.15, swap=None, commission=-0.90 (swap is None!)

sum() tries: 45.30 + (-2.50) + (-1.20) +  (-12.15) + None + (-0.90)
            = 41.60 + TypeErr: unsupported operand type(s) for +: 'float' and 'NoneType'

CRASH: Router process exits or signal handler triggers
```

**Code Gap:** No None-check before arithmetic operations.

**Recommendation (CRITICAL):**
```python
# Safe PnL calculation
net_pnl = 0.0
for p in basket_pos:
    profit = p.profit if p.profit is not None else 0.0
    swap = p.swap if p.swap is not None else 0.0
    commission = p.commission if p.commission is not None else 0.0
    
    net_pnl += profit + swap + commission
    
    # Log for debugging
    logger.debug(f"[PnL] Ticket {p.ticket}: "
                 f"profit={profit:.2f}, swap={swap:.2f}, commission={commission:.2f}")
```

---

### ⚠️ **Issue 3.1c: Unrealized vs Realized PnL Confusion**

**Problem:**
- The code uses `p.profit` which is current unrealized PnL
- But swaps and commissions are applied to BOTH open and closed portions
- For a basket that may have been partially closed, the math is inconsistent

**Trace Example:**
```
Original Basket Entry: PRIMARY 1.0 lot @ 1.3000 (AUDUSD), HEDGE 0.85 lots @ 0.7100 (NZDUSD)
Current P1: 0.6 left (0.4 closed earlier)
Current P2: 0.85 left (still full)

p1.profit = unrealized PnL on remaining 0.6 lots = +50.00
p1.swap = total accrued swaps (closed + open) = -5.00
p1.commission = total accrued commissions = -2.00

p2.profit = unrealized PnL on remaining 0.85 lots = -8.00
p2.swap = total accrued swaps = +2.00
p2.commission = total commissions = -1.50

net_pnl = (50.00 - 5.00 - 2.00) + (-8.00 + 2.00 - 1.50)
        = 43.00 + (-7.50)
        = 35.50 > 0 → Close ✓

BUT: The 0.4 lots that already closed might have had realized losses!
Realized PnL on partial close: -15.00
Current net exposure: 35.50 (unrealized) - 15.00 (realized from partial close) = 20.50 actual

The code sees 35.50 and closes, missing the fact that true total drawer is 20.50.
```

**Issue:** MT5 API doesn't cleanly separate realized/unrealized for partially closed baskets.

**Recommendation (MEDIUM):**
```python
# Consider total drawer (realized + unrealized)
# Some MT5 brokers expose p.profit_raw or p.realized_profit
if hasattr(p, 'profit_raw'):
    net_pnl += p.profit_raw + (p.swap or 0.0) + (p.commission or 0.0)
else:
    # Fallback: just use current unrealized
    net_pnl += (p.profit or 0.0) + (p.swap or 0.0) + (p.commission or 0.0)

logger.info(f"[PnL] Using {'profit_raw' if hasattr(p, 'profit_raw') else 'unrealized profit'} "
            f"for basket {basket_id}: net={net_pnl:.2f}")
```

---

### ⚠️ **Issue 3.1d: No Transaction Cost Buffer — Edge Case Overexecution**

**Problem:**
- The check is: `if net_pnl <= 0: continue` (hold)
- This means if net_pnl = 0.01, the basket CLOSES
- But if execution slippage or late swap accrual occurs, net_pnl could become negative AFTER close
- No buffer for transaction costs

**Realistic Scenario:**
```
Basket net_pnl = 0.05 (passes check: 0.05 > 0)
System issues close order
Slippage + spread cost: -0.30
Realized on close: -0.25
New net after close: -0.25 (underwater!)

If we had waited for net_pnl > 1.00 (buffer), we'd have avoided closing.
```

**Code Gap:** Hardcoded`<= 0` with no configurable buffer.

**Recommendation (MEDIUM):**
```python
MIN_PNL_TO_CLOSE = 0.50  # Configurable buffer (e.g., $0.50 or 0.50 pips)

if net_pnl < MIN_PNL_TO_CLOSE:
    logger.info(
        f"[{sym}] Z-Score target reached, but Net PnL ({net_pnl:.2f}) "
        f"is below close threshold ({MIN_PNL_TO_CLOSE}). Holding."
    )
    continue
```

---

## SUMMARY TABLE

| **Issue** | **File** | **Line(s)** | **Severity** | **Impact** |
|-----------|----------|-----------|----------|-----------|
| 1.1b: Lot step validation missing | `execution_router.py` | 588 | **CRITICAL** | HEDGE orders rejected by broker, orphaned PRIMARY position |
| 1.1c: No hedge ratio verification | `execution_router.py` | 588 | **HIGH** | Unbalanced δ-neutral basket, asymmetric exposure |
| 2.1a: Margin currency not verified | `execution_router.py` | 556 | **HIGH** | Silent margin mismatch if multi-currency brokers used |
| 2.1b: Margin None not handled | `execution_router.py` | 556 | **CRITICAL** | Margin validation bypassed, overleveraging possible |
| 3.1a: Swap/commission sign assumptions | `execution_router.py` | 428 | **HIGH** | Wrong net PnL if broker variant has opposite signs |
| 3.1b: Swap/commission may be None | `execution_router.py` | 428 | **CRITICAL** | TypeError crash when closing Mean Reversion exits |
| 3.1c: Unrealized vs realized confusion | `execution_router.py` | 428 | **MEDIUM** | Partial closes skew PnL accounting  |
| 3.1d: No transaction cost buffer | `execution_router.py` | 431 | **MEDIUM** | Edge case: close on razor-thin margin, slippage underwater exit |

---

## PRIORITY FIXES

**IMMEDIATE (Prevent crashes):**
1. Add None-checks on `p.swap` and `p.commission` → Issue 3.1b
2. Validate margin calculations return non-None → Issue 2.1b
3. Add volume_step validation for HEDGE lots → Issue 1.1b

**HIGH (Prevent silent failures):**
1. Log margin currency & verify consistency → Issue 2.1a
2. Verify hedge ratio after rounding → Issue 1.1c
3. Check swap/commission signs → Issue 3.1a

**MEDIUM (Robustness):**
1. Add realized vs unrealized PnL logic → Issue 3.1c
2. Implement min PnL buffer for exits → Issue 3.1d
