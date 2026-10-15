"""
amm_math.py - Pure (no I/O) AMM pricing helpers.

Everything here works on raw integer token units (wei-style), mirroring on-chain
integer arithmetic so results can be compared 1:1 with what the contracts do.

Conventions
-----------
* token0 / token1 are the canonical address-sorted tokens used by BOTH Uniswap V2
  forks (Sushi) and Uniswap V3.
* "price" below means raw token1 per raw token0 unless stated otherwise.
"""
from __future__ import annotations

from typing import Callable

Q96 = 2**96


# --------------------------------------------------------------------------- #
# Uniswap V2 / SushiSwap V2: constant product  x * y = k
# --------------------------------------------------------------------------- #
def v2_amount_out(amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int = 30) -> int:
    """
    Exact V2 `getAmountOut`:  out = in*(1-f)*Rout / (Rin + in*(1-f))
    Integer form used on-chain (fee 0.30% -> 997/1000).
    """
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    amount_in_with_fee = amount_in * (10_000 - fee_bps)
    return (amount_in_with_fee * reserve_out) // (reserve_in * 10_000 + amount_in_with_fee)


def v2_spot_price(reserve0: int, reserve1: int) -> float:
    """Marginal price (token1 per token0, raw units), ignoring fee and impact."""
    return reserve1 / reserve0


# --------------------------------------------------------------------------- #
# Uniswap V3: concentrated liquidity, price stored as sqrtPriceX96
# --------------------------------------------------------------------------- #
def sqrt_price_x96_to_price(sqrt_price_x96: int) -> float:
    """Raw token1 per raw token0:  P = (sqrtPriceX96 / 2^96)^2."""
    return (sqrt_price_x96 / Q96) ** 2


def tick_to_sqrt_price_x96(tick: int) -> int:
    """
    sqrtP = 1.0001^(tick/2). Float implementation: precise enough for the
    'did we leave the current tick range?' check (NOT used for amounts).
    """
    return int((1.0001 ** (tick / 2)) * Q96)


def v3_swap_in_range(
    sqrt_price_x96: int,
    liquidity: int,
    amount_in: int,
    fee_pips: int,
    zero_for_one: bool,
) -> tuple[int, int]:
    """
    Single-step Uniswap V3 swap assuming liquidity L is constant (no initialized
    tick is crossed). Derivation (real-number sqrtP = s / 2^96):

      token0 in (price falls):   1/sqrtP' = 1/sqrtP + dx/L
                                 dy = L * (sqrtP - sqrtP')
      token1 in (price rises):   sqrtP'   = sqrtP + dy/L
                                 dx = L * (1/sqrtP - 1/sqrtP')

    Returns (amount_out, new_sqrt_price_x96). The fee (in pips, 3000 = 0.30%)
    is taken from the input before it moves the price.
    """
    if amount_in <= 0 or liquidity <= 0:
        return 0, sqrt_price_x96

    amount_in_less_fee = amount_in * (1_000_000 - fee_pips) // 1_000_000

    if zero_for_one:
        # new = L*Q96*s / (L*Q96 + dx*s)
        new_sqrt = (liquidity * Q96 * sqrt_price_x96) // (liquidity * Q96 + amount_in_less_fee * sqrt_price_x96)
        amount_out = liquidity * (sqrt_price_x96 - new_sqrt) // Q96
    else:
        new_sqrt = sqrt_price_x96 + (amount_in_less_fee * Q96) // liquidity
        amount_out = (liquidity * Q96 * (new_sqrt - sqrt_price_x96)) // (new_sqrt * sqrt_price_x96)

    return amount_out, new_sqrt


def v3_leaves_tick_range(
    tick: int, tick_spacing: int, new_sqrt_price_x96: int, zero_for_one: bool
) -> bool:
    """
    Conservative check: True if the swap moved price past the boundary of the
    current tick-spacing bucket. Liquidity MAY change there (if a tick is
    initialized), so the local quote should then be confirmed with QuoterV2.
    """
    lower = (tick // tick_spacing) * tick_spacing  # floor division handles negatives
    upper = lower + tick_spacing
    if zero_for_one:
        return new_sqrt_price_x96 < tick_to_sqrt_price_x96(lower)
    return new_sqrt_price_x96 >= tick_to_sqrt_price_x96(upper)


# --------------------------------------------------------------------------- #
# Optimizer
# --------------------------------------------------------------------------- #
def maximize_unimodal(fn: Callable[[int], int], lo: int, hi: int, iters: int = 80) -> tuple[int, int]:
    """
    Integer ternary search for the maximum of a concave/unimodal function.
    Round-trip arbitrage profit f(a) = out(a) - a is concave in the input
    size for constant-product style curves, so this finds the optimal loan.
    """
    for _ in range(iters):
        if hi - lo <= 3:
            break
        third = (hi - lo) // 3
        m1, m2 = lo + third, hi - third
        if fn(m1) < fn(m2):
            lo = m1
        else:
            hi = m2
    best = max(range(lo, hi + 1), key=fn)
    return best, fn(best)


__all__ = [
    "Q96",
    "v2_amount_out",
    "v2_spot_price",
    "sqrt_price_x96_to_price",
    "tick_to_sqrt_price_x96",
    "v3_swap_in_range",
    "v3_leaves_tick_range",
    "maximize_unimodal",
]
