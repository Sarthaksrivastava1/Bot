# FlashArb: Balancer flash loan, Uniswap V3 <-> SushiSwap spatial arbitrage

```
flash-arb/
├── contracts/FlashArb.sol     on-chain executor (Balancer V2 flash loan + 2 swaps + profit gate)
├── scripts/deploy.js          deploy, register routes, authorise bot wallet, hand over ownership
├── hardhat.config.js          Arbitrum + Base networks
├── bot/amm_math.py            pure AMM math (V2 constant product, V3 sqrtPriceX96, optimizer)
├── bot/arb_bot.py             async scanner / evaluator / executor (+ paper-trading mode)
├── bot/requirements.txt
└── .env.example               every setting, with Arbitrum and Base addresses
```

## 1. How one attempt works

```
Python bot (off-chain)                         FlashArb.sol (one atomic tx)
──────────────────────                         ───────────────────────────────────────────
poll Uni V3 slot0 + Sushi reserves             executeArbitrage(routeId, amount, ...)   [operator only]
  │ optimal size via ternary search              │  stores keccak(userData)
  │ spread − (fees+slippage) − gas > min?        ▼
  │ exact re-quote (QuoterV2/getAmountsOut)    Balancer Vault.flashLoan()   0% fee
  │ eth_estimateGas dry-run of the real call     │
  ▼                                              ▼
sign + send  ───────────────────────────────►  receiveFlashLoan()
                                                  ├ msg.sender == Vault ? hash matches ? (else revert)
                                                  ├ leg A: loan token → target   (Uni V3 or Sushi, per route)
                                                  ├ leg B: target → loan token   (the other venue)
                                                  ├ out <= principal + fee + gas + minProfit ? revert ArbitrageNotProfitable()
                                                  └ transfer principal(+fee) back to Vault
                                               profit stays in the contract → owner.withdraw()
```

Any revert unwinds the loans and both swaps; the only cost of a failed attempt is gas.

**Routes** are stored in the contract (`setRoute`): loan token, target token, Uniswap fee tier and venue order.
The bot only sends a `routeId`, so a caller can never inject a token, router or path.

| id | loan | order |
|----|------|-------|
| 0 | USDC | buy WETH on Uniswap V3, sell on Sushi |
| 1 | USDC | buy WETH on Sushi, sell on Uniswap V3 |
| 2 | WETH | buy USDC on Uniswap V3, sell on Sushi |
| 3 | WETH | buy USDC on Sushi, sell on Uniswap V3 |

## 2. Environment setup

Prerequisites: Node 18+/20+, Python 3.10+.

```bash
# contracts
npm install                      # hardhat@2, hardhat-toolbox (ethers v6), @openzeppelin/contracts@5, dotenv
npx hardhat compile

# bot
python -m venv .venv && source .venv/bin/activate
pip install -r bot/requirements.txt     # web3, python-dotenv  (eth-account and aiohttp come with web3)

cp .env.example .env             # then fill it in
```

### Key handling: three roles, three trust levels

| Role | Where it lives | Power |
|------|----------------|-------|
| **Owner** | hardware wallet or multisig (only its *address* goes in `.env`) | `withdraw`, `setRoute`, `setOperator` |
| **Deployer** | throwaway key in `DEPLOYER_PRIVATE_KEY`, used once, then retire | deploys, then hands ownership over (`Ownable2Step`: owner must call `acceptOwnership()`) |
| **Operator** | the bot's hot key in `PRIVATE_KEY`, funded with ~0.01–0.05 ETH | can *only* call `executeArbitrage` |

A leaked bot key can therefore waste gas but cannot move profits.
Rules: `.env` is in `.gitignore`; never paste keys into code, logs or chat; use separate RPC keys per environment;
on a server prefer a secret manager (AWS Secrets Manager, GCP Secret Manager, Vault) injected as env vars over a file on disk;
sweep profits to the owner wallet regularly so little value sits in the contract.

## 3. Configuring for Arbitrum or Base

1. Set `CHAIN`, `RPC_URL`, `ARBITRUM_RPC_URL`/`BASE_RPC_URL` and the address block for that chain in `.env`
   (Base values are provided commented-out). **Verify every address on the official docs / explorer**; they were filled from memory.
2. Check the pair exists on **both** venues in the exact token you intend to trade. On Arbitrum, many SushiSwap pools use bridged
   `USDC.e`, not native USDC; `setup()` aborts with a clear message if the V2 pair is missing.
3. Gas model (the main difference from mainnet):
   * **Arbitrum:** the L1 data cost is folded into `eth_estimateGas`; gas price is ~0.01–0.1 gwei. The bot needs no extra term.
   * **Base (OP stack):** total cost = L2 gas × price **+ L1 data fee**, which the bot reads from the `GasPriceOracle` predeploy
     (`getL1Fee`). Ignoring it is the classic way L2 bots lose money on "profitable" trades.
   * Both are converted into loan-token units and passed to the contract as `estimatedGasCost`, so the on-chain gate enforces it.
4. Flash-loan size is capped by what the Balancer Vault holds of that token on that chain (the bot uses 95% of it, or `MAX_LOAN`).

## 4. Deployment procedure

```bash
# 1. fork-test first (needs a fork-capable RPC): spin up a local fork and run a dry run against it
npx hardhat node --fork $ARBITRUM_RPC_URL

# 2. deploy (set OWNER_ADDRESS + OPERATOR_ADDRESS in .env first)
npm run deploy:arbitrum        # or: npm run deploy:base
#    → prints ARB_CONTRACT=0x…  copy it into .env
#    → owner then calls acceptOwnership() from the hardware wallet / multisig

# 3. paper trade (default PAPER_TRADING=true): simulates everything, broadcasts nothing
cd bot && python arb_bot.py
#    results accumulate in paper_ledger.csv (statuses: PAPER_WOULD_EXECUTE, REJECT_*)

# 4. only after you've studied the ledger: PAPER_TRADING=false with a small MAX_LOAN
```

Contract verification: set `VERIFY=true` and an explorer API key, or run `npx hardhat verify --network arbitrum <addr> <vault> <uniRouter> <sushiRouter>`.

## 5. Anti-front-running design (and its limits)

Built in:
* paths, tokens and fee tiers live in contract storage; callers pass only a `routeId`
* `executeArbitrage` is operator-gated, and the flash-loan callback accepts only a hash-matched call from the Vault within the same tx
  (Balancer lets *anyone* point a flash loan at your contract, so this check matters)
* exact approvals, reset to 0 after each swap; balance-delta accounting; `ReentrancyGuard`
* atomic profit gate with `ArbitrageNotProfitable()` and a `minTargetOut` floor on leg A
* the bot re-quotes with the real on-chain quoters and dry-runs the actual contract call before signing

Limits you should understand: on Arbitrum and Base there is no public mempool (a single sequencer orders transactions), so classic
sandwiching is much less of a concern than on Ethereum mainnet, and Flashbots Protect is not an L2 tool. The real adversary there is
**other arbitrage bots that are faster than you** (sequencer latency on Arbitrum, priority-fee bidding on Base). Use a dedicated,
low-latency RPC close to the sequencer and submit through it directly.

## 6. Realistic expectations (please read before going live)

* **Fee stack:** Uniswap V3 0.05% + Sushi V2 0.30% means the two prices must differ by more than ~0.35% *before* price impact and gas.
  On liquid pairs like WETH/USDC, spreads that large are rare and are usually captured within the same block by faster bots.
* **Thin Sushi liquidity on L2s** limits profitable size, which limits profit per trade.
* **Paper trading overstates results.** It cannot model losing the race to a competitor, inclusion latency, or state changing between
  your simulation and execution. Treat `PAPER_WOULD_EXECUTE` rows as an upper bound, and expect live hit rates to be materially lower.
* Failed attempts still cost gas, so a bot that fires often but wins rarely loses money. Watch gas spent vs. profit on any live run.
* Directions to improve if the ledger shows promise: subscribe to new blocks over WebSocket instead of polling, add more pairs and
  fee tiers, add Sushi V3 pools, and run close to the sequencer.

## 7. Status of this code

* `FlashArb.sol` compiles cleanly (solc 0.8.24, optimizer on). The AMM math and `evaluate()` were exercised offline against synthetic markets.
* **Not yet run against a live RPC, on a fork, or on-chain, and not audited.** Add Hardhat fork tests (happy path, `ArbitrageNotProfitable`,
  unauthorised callback, withdraw permissions) before deploying anything with real funds, and start with small amounts.
* The local V3 quote is exact only while the swap stays inside the current tick range; the bot flags `tickCross` and always replaces it
  with the QuoterV2 result before sending.
* Written against web3.py 7/8 (async API) and Uniswap's standard `slot0` layout.
