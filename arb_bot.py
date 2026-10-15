"""
arb_bot.py - Async scanner + executor for the FlashArb contract.

Pipeline per poll (all reads pinned to ONE block for a consistent snapshot):
  1. snapshot()   - Uniswap V3 slot0/liquidity, Sushi V2 reserves, gas price, Balancer liquidity
  2. evaluate()   - local AMM math (constant product + sqrtPriceX96) -> optimal loan size and
                    (price discrepancy) - (fees + slippage) - (gas) vs. MIN_PROFIT
  3. handle()     - exact re-quote on-chain (QuoterV2 / getAmountsOut), eth_estimateGas dry-run
                    of the real contract call, final gas-aware profit check, then
                    PAPER_TRADING=true  -> write a hypothetical trade to the CSV ledger
                    PAPER_TRADING=false -> sign + broadcast executeArbitrage()

Assumptions: BASE token is WETH (gas is paid in ETH, so WETH<->wei conversion is 1:1).
"""
from __future__ import annotations

import asyncio
import csv
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from eth_account import Account
from web3 import AsyncHTTPProvider, AsyncWeb3

from amm_math import (
    maximize_unimodal,
    sqrt_price_x96_to_price,
    v2_amount_out,
    v3_leaves_tick_range,
    v3_swap_in_range,
)

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
log = logging.getLogger("arb")

ZERO_ADDR = "0x0000000000000000000000000000000000000000"
OP_GAS_ORACLE = "0x420000000000000000000000000000000000000F"  # OP-stack (Base) L1 fee oracle


# --------------------------------------------------------------------------- #
# Minimal ABIs
# --------------------------------------------------------------------------- #
def _fn(name, ins, outs, mut="view"):
    return {
        "type": "function",
        "name": name,
        "stateMutability": mut,
        "inputs": [{"name": f"i{i}", "type": t} for i, t in enumerate(ins)],
        "outputs": [{"name": f"o{i}", "type": t} for i, t in enumerate(outs)],
    }


ERC20_ABI = [_fn("balanceOf", ["address"], ["uint256"]), _fn("decimals", [], ["uint8"])]
V3_FACTORY_ABI = [_fn("getPool", ["address", "address", "uint24"], ["address"])]
V3_POOL_ABI = [
    _fn("slot0", [], ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"]),
    _fn("liquidity", [], ["uint128"]),
    _fn("tickSpacing", [], ["int24"]),
    _fn("token0", [], ["address"]),
]
V2_ROUTER_ABI = [
    _fn("factory", [], ["address"]),
    _fn("getAmountsOut", ["uint256", "address[]"], ["uint256[]"]),
]
V2_FACTORY_ABI = [_fn("getPair", ["address", "address"], ["address"])]
V2_PAIR_ABI = [_fn("getReserves", [], ["uint112", "uint112", "uint32"]), _fn("token0", [], ["address"])]
QUOTER_V2_ABI = [
    {
        "type": "function",
        "name": "quoteExactInputSingle",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
            }
        ],
        "outputs": [
            {"name": "amountOut", "type": "uint256"},
            {"name": "sqrtPriceX96After", "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate", "type": "uint256"},
        ],
    }
]
ARB_ABI = [_fn("executeArbitrage", ["uint256", "uint256", "uint256", "uint256", "uint256"], [], "nonpayable")]
OP_ORACLE_ABI = [_fn("getL1Fee", ["bytes"], ["uint256"])]


# --------------------------------------------------------------------------- #
# Configuration (.env)
# --------------------------------------------------------------------------- #
def _env(name: str, default: str | None = None) -> str:
    v = os.getenv(name, default)
    if v is None or v == "":
        raise SystemExit(f"Missing required environment variable: {name}")
    return v


@dataclass
class Config:
    chain: str
    rpc_url: str
    private_key: str | None
    operator_address: str | None  # used as `from` for simulations when no key is loaded (paper mode)
    arb_contract: str
    base_token: str  # WETH
    quote_token: str  # USDC
    loan_is_quote: bool  # borrow the quote token (USDC) if True, else the base token (WETH)
    uni_factory: str
    uni_quoter: str
    uni_fee: int
    sushi_router: str
    balancer_vault: str
    min_profit: float  # human units of the loan token
    max_loan: float  # human units of the loan token
    poll_interval: float
    paper: bool
    slippage_bps: int
    gas_buffer: float
    priority_fee_wei: int
    gas_units_guess: int
    ledger_path: Path

    @classmethod
    def from_env(cls) -> "Config":
        cs = AsyncWeb3.to_checksum_address
        paper = _env("PAPER_TRADING", "true").lower() != "false"  # default: SAFE (paper)
        key = os.getenv("PRIVATE_KEY") or None
        op = os.getenv("OPERATOR_ADDRESS") or None
        if not paper and not key:
            raise SystemExit("PRIVATE_KEY is required when PAPER_TRADING=false")
        if paper and not key and not op:
            raise SystemExit("Paper mode needs PRIVATE_KEY or OPERATOR_ADDRESS (an authorized operator) to simulate calls")
        return cls(
            chain=_env("CHAIN", "arbitrum").lower(),
            rpc_url=_env("RPC_URL"),
            private_key=key,
            operator_address=cs(op) if op else None,
            arb_contract=cs(_env("ARB_CONTRACT")),
            base_token=cs(_env("TOKEN_BASE")),
            quote_token=cs(_env("TOKEN_QUOTE")),
            loan_is_quote=_env("LOAN_IN", "QUOTE").upper() == "QUOTE",
            uni_factory=cs(_env("UNI_V3_FACTORY")),
            uni_quoter=cs(_env("UNI_QUOTER_V2")),
            uni_fee=int(_env("UNI_FEE", "500")),
            sushi_router=cs(_env("SUSHI_ROUTER")),
            balancer_vault=cs(_env("BALANCER_VAULT", "0xBA12222222228d8Ba445958a75a0704d566BF2C8")),
            min_profit=float(_env("MIN_PROFIT", "2")),
            max_loan=float(_env("MAX_LOAN", "50000")),
            poll_interval=float(_env("POLL_INTERVAL", "1.0")),
            paper=paper,
            slippage_bps=int(_env("SLIPPAGE_BPS", "30")),
            gas_buffer=float(_env("GAS_BUFFER", "0.2")),
            priority_fee_wei=int(_env("PRIORITY_FEE_WEI", "1000000")),
            gas_units_guess=int(_env("GAS_UNITS_GUESS", "450000")),
            ledger_path=Path(_env("LEDGER_PATH", "paper_ledger.csv")),
        )


# --------------------------------------------------------------------------- #
# Data containers
# --------------------------------------------------------------------------- #
@dataclass
class Market:
    block: int
    sqrt_price_x96: int
    tick: int
    liquidity: int
    v2_r0: int
    v2_r1: int
    gas_price: int
    vault_balance: int  # Balancer's balance of the loan token = max borrowable


@dataclass
class Opportunity:
    block: int
    uni_first: bool
    route_id: int
    loan_amount: int
    expected_out: int
    gross_edge: int  # (market price discrepancy) * size, before fees/impact
    fees_and_slippage: int  # gross_edge - actual gross profit (pool fees + price impact)
    gas_cost: int  # estimated gas in loan-token units
    net: int  # expected_out - loan_amount - gas_cost
    spread_bps: float
    may_cross_tick: bool


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #
class ArbBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.w3 = AsyncWeb3(AsyncHTTPProvider(cfg.rpc_url))
        self.account = Account.from_key(cfg.private_key) if cfg.private_key else None
        self._last_attempt_block = -1

    # ------------------------------ setup ------------------------------- #
    async def setup(self) -> None:
        w3, cfg = self.w3, self.cfg
        chain_id = await w3.eth.chain_id
        log.info("connected: chain_id=%s paper=%s", chain_id, cfg.paper)
        self.chain_id = chain_id

        c = lambda addr, abi: w3.eth.contract(address=addr, abi=abi)  # noqa: E731
        self.base_c, self.quote_c = c(cfg.base_token, ERC20_ABI), c(cfg.quote_token, ERC20_ABI)
        self.dec_base = await self.base_c.functions.decimals().call()
        self.dec_quote = await self.quote_c.functions.decimals().call()

        # Uniswap V3 pool for (base, quote, fee)
        pool_addr = await c(cfg.uni_factory, V3_FACTORY_ABI).functions.getPool(
            cfg.base_token, cfg.quote_token, cfg.uni_fee
        ).call()
        if pool_addr == ZERO_ADDR:
            raise SystemExit("No Uniswap V3 pool for this pair/fee tier")
        self.uni_pool = c(pool_addr, V3_POOL_ABI)
        self.token0 = (await self.uni_pool.functions.token0().call()).lower()
        self.tick_spacing = await self.uni_pool.functions.tickSpacing().call()
        self.base_is_token0 = cfg.base_token.lower() == self.token0

        # SushiSwap V2 pair, discovered through the router's factory
        self.sushi_router = c(cfg.sushi_router, V2_ROUTER_ABI)
        v2_factory = await self.sushi_router.functions.factory().call()
        pair_addr = await c(v2_factory, V2_FACTORY_ABI).functions.getPair(cfg.base_token, cfg.quote_token).call()
        if pair_addr == ZERO_ADDR:
            raise SystemExit("No SushiSwap V2 pair for this token pair")
        self.sushi_pair = c(pair_addr, V2_PAIR_ABI)
        assert (await self.sushi_pair.functions.token0().call()).lower() == self.token0

        self.quoter = c(cfg.uni_quoter, QUOTER_V2_ABI)
        self.arb = c(cfg.arb_contract, ARB_ABI)
        self.l1_oracle = c(OP_GAS_ORACLE, OP_ORACLE_ABI) if cfg.chain == "base" else None

        # Token roles for the chosen loan direction
        self.loan_tok = cfg.quote_token if cfg.loan_is_quote else cfg.base_token
        self.target_tok = cfg.base_token if cfg.loan_is_quote else cfg.quote_token
        self.loan_dec = self.dec_quote if cfg.loan_is_quote else self.dec_base
        self.loan_c = self.quote_c if cfg.loan_is_quote else self.base_c
        self.min_profit_raw = int(cfg.min_profit * 10**self.loan_dec)
        self.max_loan_raw = int(cfg.max_loan * 10**self.loan_dec)
        self.min_loan_raw = max(10**self.loan_dec // 10, 1)  # search floor (0.1 token)
        # Must mirror scripts/deploy.js: USDC-loan routes = 0/1, WETH-loan routes = 2/3
        self.route_base = 0 if cfg.loan_is_quote else 2
        log.info("uni pool=%s sushi pair=%s loan=%s", pool_addr, pair_addr, self.loan_tok)

    # ----------------------------- snapshot ----------------------------- #
    async def snapshot(self) -> Market:
        block = await self.w3.eth.block_number
        bi = block  # pin every call to the same block -> internally consistent state
        slot0, liq, reserves, gas_price, vault_bal = await asyncio.gather(
            self.uni_pool.functions.slot0().call(block_identifier=bi),
            self.uni_pool.functions.liquidity().call(block_identifier=bi),
            self.sushi_pair.functions.getReserves().call(block_identifier=bi),
            self.w3.eth.gas_price,
            self.loan_c.functions.balanceOf(self.cfg.balancer_vault).call(block_identifier=bi),
        )
        return Market(block, slot0[0], slot0[1], liq, reserves[0], reserves[1], gas_price, vault_bal)

    # ------------------------- local pricing math ------------------------ #
    def _uni_out(self, m: Market, token_in: str, amt: int) -> tuple[int, bool]:
        z4o = token_in.lower() == self.token0
        out, new = v3_swap_in_range(m.sqrt_price_x96, m.liquidity, amt, self.cfg.uni_fee, z4o)
        return out, v3_leaves_tick_range(m.tick, self.tick_spacing, new, z4o)

    def _sushi_out(self, m: Market, token_in: str, amt: int) -> int:
        if token_in.lower() == self.token0:
            return v2_amount_out(amt, m.v2_r0, m.v2_r1)
        return v2_amount_out(amt, m.v2_r1, m.v2_r0)

    def _price_target_in_loan(self, m: Market) -> tuple[float, float]:
        """Spot price of the target token denominated in the loan token: (uniswap, sushi)."""
        p_uni = sqrt_price_x96_to_price(m.sqrt_price_x96)  # raw token1 per raw token0
        p_v2 = m.v2_r1 / m.v2_r0
        loan_is_token1 = self.loan_tok.lower() != self.token0
        if loan_is_token1:  # target = token0 -> price already "loan per target"
            return p_uni, p_v2
        return 1 / p_uni, 1 / p_v2

    def _price_base_in_quote_raw(self, m: Market) -> float:
        p = sqrt_price_x96_to_price(m.sqrt_price_x96)
        return p if self.base_is_token0 else 1 / p

    def _wei_to_loan(self, wei: int, m: Market) -> int:
        """Convert an ETH gas cost (wei) into loan-token raw units."""
        if not self.cfg.loan_is_quote:
            return wei  # loan token is WETH
        return int(wei * self._price_base_in_quote_raw(m))  # raw USDC per raw WETH

    # ------------------------------ evaluate ----------------------------- #
    def evaluate(self, m: Market) -> Opportunity | None:
        """
        net = gross_edge - (fees + slippage) - gas   must exceed MIN_PROFIT
        Both venue orders are tested; the optimal loan size is found per order.
        """
        loan, target = self.loan_tok, self.target_tok
        max_in = min(self.max_loan_raw, m.vault_balance * 95 // 100)
        if max_in <= self.min_loan_raw:
            return None

        p_uni, p_sushi = self._price_target_in_loan(m)
        gas_cost = self._wei_to_loan(self.cfg.gas_units_guess * (m.gas_price + self.cfg.priority_fee_wei), m)

        best: Opportunity | None = None
        for uni_first in (True, False):

            def round_trip(a: int, uni_first: bool = uni_first) -> int:
                if uni_first:  # buy target on Uniswap, sell on Sushi
                    t, _ = self._uni_out(m, loan, a)
                    return self._sushi_out(m, target, t)
                t = self._sushi_out(m, loan, a)  # buy target on Sushi, sell on Uniswap
                return self._uni_out(m, target, t)[0]

            amount, gross_profit = maximize_unimodal(lambda a: round_trip(a) - a, self.min_loan_raw, max_in)
            if gross_profit <= 0:
                continue

            buy_p, sell_p = (p_uni, p_sushi) if uni_first else (p_sushi, p_uni)
            edge_ratio = sell_p / buy_p - 1  # market price discrepancy (fraction)
            gross_edge = int(amount * edge_ratio)
            fees_slip = gross_edge - gross_profit
            net = gross_profit - gas_cost

            # Does the winning size leave the current Uniswap tick bucket?
            if uni_first:
                crossed = self._uni_out(m, loan, amount)[1]
            else:
                crossed = self._uni_out(m, target, self._sushi_out(m, loan, amount))[1]

            opp = Opportunity(
                block=m.block,
                uni_first=uni_first,
                route_id=self.route_base + (0 if uni_first else 1),
                loan_amount=amount,
                expected_out=amount + gross_profit,
                gross_edge=gross_edge,
                fees_and_slippage=fees_slip,
                gas_cost=gas_cost,
                net=net,
                spread_bps=edge_ratio * 1e4,
                may_cross_tick=crossed,
            )
            if best is None or opp.net > best.net:
                best = opp

        if best and best.net > self.min_profit_raw:  # gross_edge - (slippage+fees) - gas > MIN_PROFIT
            return best
        return None

    # --------------------- exact on-chain re-quote ---------------------- #
    async def _exact_uni(self, tin: str, tout: str, amt: int, block: int | str) -> int:
        res = await self.quoter.functions.quoteExactInputSingle(
            (tin, tout, amt, self.cfg.uni_fee, 0)
        ).call(block_identifier=block)
        return res[0]

    async def _exact_sushi(self, tin: str, tout: str, amt: int, block: int | str) -> int:
        res = await self.sushi_router.functions.getAmountsOut(amt, [tin, tout]).call(block_identifier=block)
        return res[1]

    async def exact_round_trip(self, opp: Opportunity) -> tuple[int, int]:
        """Returns (leg_A_output, final_loan_token_output) using the pools' real on-chain logic."""
        loan, target, a = self.loan_tok, self.target_tok, opp.loan_amount
        if opp.uni_first:
            t = await self._exact_uni(loan, target, a, "latest")
            out = await self._exact_sushi(target, loan, t, "latest")
        else:
            t = await self._exact_sushi(loan, target, a, "latest")
            out = await self._exact_uni(target, loan, t, "latest")
        return t, out

    # ------------------------- gas / L2 cost model ------------------------ #
    async def _l1_fee_wei(self, calldata: bytes, signed_raw: bytes | None) -> int:
        """
        L2 cost = L2 execution gas + L1 data-posting fee.
        * Arbitrum: the L1 component is already folded into eth_estimateGas, return 0.
        * Base (OP stack): ask the GasPriceOracle predeploy.
        """
        if self.l1_oracle is None:
            return 0
        payload = signed_raw if signed_raw is not None else calldata + b"\x01" * 110  # approx. tx envelope
        return await self.l1_oracle.functions.getL1Fee(payload).call()

    async def _build_tx(self, opp: Opportunity, min_target: int, gas_cost: int, min_profit: int, gas_limit: int | None):
        sender = self.account.address if self.account else self.cfg.operator_address
        fn = self.arb.functions.executeArbitrage(opp.route_id, opp.loan_amount, min_target, gas_cost, min_profit)
        params = {"from": sender}
        if gas_limit is None:
            gas_limit = await fn.estimate_gas(params)  # raises if the contract would revert
        gas_price = await self.w3.eth.gas_price
        tx = await fn.build_transaction(
            {
                "from": sender,
                "nonce": await self.w3.eth.get_transaction_count(sender, "pending"),
                "gas": gas_limit,
                "maxFeePerGas": int(gas_price * 1.5) + self.cfg.priority_fee_wei,
                "maxPriorityFeePerGas": self.cfg.priority_fee_wei,
                "chainId": self.chain_id,
            }
        )
        return tx, gas_limit

    # ------------------------------ handle ------------------------------- #
    async def handle(self, opp: Opportunity, m: Market) -> None:
        if opp.block == self._last_attempt_block:
            return
        self._last_attempt_block = opp.block
        cfg = self.cfg
        log.info(
            "CANDIDATE blk=%s route=%s size=%.2f spread=%.1fbps gross=%.4f fee+slip=%.4f gas=%.4f net=%.4f tickCross=%s",
            opp.block, opp.route_id, opp.loan_amount / 10**self.loan_dec, opp.spread_bps,
            opp.gross_edge / 10**self.loan_dec, opp.fees_and_slippage / 10**self.loan_dec,
            opp.gas_cost / 10**self.loan_dec, opp.net / 10**self.loan_dec, opp.may_cross_tick,
        )

        # 1) Exact on-chain quote (replaces the approximation, esp. if a tick was crossed)
        leg_a_out, exact_out = await self.exact_round_trip(opp)
        if exact_out <= opp.loan_amount:
            self._record(opp, "REJECT_EXACT_QUOTE_UNPROFITABLE", "")
            return
        min_target = leg_a_out * (10_000 - cfg.slippage_bps) // 10_000

        # 2) Dry-run the REAL contract call (zero gas/min-profit margins) to size the gas
        try:
            tx0, gas_limit = await self._build_tx(opp, min_target, 0, 0, None)
        except Exception as e:  # revert in simulation => ArbitrageNotProfitable or stale price
            self._record(opp, "REJECT_SIM_REVERT", str(e)[:120])
            return

        # 3) Real cost model: L2 gas (+buffer) and, on Base, the L1 data fee
        signed_raw = self.account.sign_transaction(tx0).raw_transaction if self.account else None
        l1_fee = await self._l1_fee_wei(bytes.fromhex(tx0["data"][2:]), signed_raw)
        gas_wei = int(gas_limit * (1 + cfg.gas_buffer)) * (m.gas_price + cfg.priority_fee_wei) + l1_fee
        gas_cost = self._wei_to_loan(gas_wei, m)
        net = exact_out - opp.loan_amount - gas_cost
        if net <= self.min_profit_raw:
            self._record(opp, "REJECT_NET_BELOW_THRESHOLD", f"net={net / 10**self.loan_dec:.4f}")
            return

        # 4) Final transaction with the on-chain profit gate armed (gas + MIN_PROFIT)
        try:
            tx, _ = await self._build_tx(opp, min_target, gas_cost, self.min_profit_raw, int(gas_limit * 1.25))
        except Exception as e:
            self._record(opp, "REJECT_GATE_WOULD_REVERT", str(e)[:120])
            return

        if cfg.paper:
            self._record(opp, "PAPER_WOULD_EXECUTE", f"net={net / 10**self.loan_dec:.4f}", net=net, gas_cost=gas_cost)
            return

        signed = self.account.sign_transaction(tx)
        tx_hash = await self.w3.eth.send_raw_transaction(signed.raw_transaction)
        log.info("broadcast %s", tx_hash.hex())
        receipt = await self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        status = "LIVE_SUCCESS" if receipt["status"] == 1 else "LIVE_REVERTED"
        self._record(opp, status, "", net=net, gas_cost=gas_cost, tx_hash=tx_hash.hex())

    # ------------------------------ ledger ------------------------------- #
    def _record(self, opp: Opportunity, status: str, note: str, net: int | None = None,
                gas_cost: int | None = None, tx_hash: str = "") -> None:
        d = 10**self.loan_dec
        row = [
            int(time.time()), opp.block, opp.route_id, opp.loan_amount / d, opp.expected_out / d,
            opp.gross_edge / d, opp.fees_and_slippage / d,
            (gas_cost if gas_cost is not None else opp.gas_cost) / d,
            (net if net is not None else opp.net) / d, status, tx_hash, note,
        ]
        new = not self.cfg.ledger_path.exists()
        with self.cfg.ledger_path.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["ts", "block", "route", "loan", "expected_out", "gross_edge", "fees_slippage",
                            "gas_cost", "net", "status", "tx_hash", "note"])
            w.writerow(row)
        log.info("%s %s", status, note)

    # ------------------------------- loop -------------------------------- #
    async def run(self) -> None:
        await self.setup()
        polls = 0
        while True:
            t0 = time.monotonic()
            try:
                m = await self.snapshot()
                opp = self.evaluate(m)
                if opp:
                    await self.handle(opp, m)
                elif polls % 30 == 0:
                    p_u, p_s = self._price_target_in_loan(m)
                    log.info("blk=%s no edge | uni=%.8g sushi=%.8g spread=%.1fbps",
                             m.block, p_u, p_s, abs(p_u / p_s - 1) * 1e4)
            except Exception:
                log.exception("poll failed")
            polls += 1
            await asyncio.sleep(max(0.0, self.cfg.poll_interval - (time.monotonic() - t0)))


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(ArbBot(Config.from_env()).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
