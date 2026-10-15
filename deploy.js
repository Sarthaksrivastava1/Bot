/**
 * Deploys FlashArb, registers the 4 routes the Python bot expects, authorises the
 * bot's hot wallet as an *operator*, and (optionally) starts an ownership handover.
 *
 *   npx hardhat run scripts/deploy.js --network arbitrum   (or base)
 *
 * Route ids (must match bot/arb_bot.py):
 *   0  loan QUOTE (USDC): buy WETH on Uniswap V3, sell on Sushi
 *   1  loan QUOTE (USDC): buy WETH on Sushi,      sell on Uniswap V3
 *   2  loan BASE  (WETH): buy USDC on Uniswap V3, sell on Sushi
 *   3  loan BASE  (WETH): buy USDC on Sushi,      sell on Uniswap V3
 */
require("dotenv").config();
const { ethers, network, run } = require("hardhat");

const need = (k) => {
  if (!process.env[k]) throw new Error(`Missing required env var: ${k}`);
  return process.env[k];
};

async function main() {
  const [deployer] = await ethers.getSigners();
  console.log(`Network: ${network.name} | deployer: ${deployer.address}`);

  const vault = process.env.BALANCER_VAULT || "0xBA12222222228d8Ba445958a75a0704d566BF2C8";
  const uniRouter = need("UNI_SWAP_ROUTER02");
  const sushiRouter = need("SUSHI_ROUTER");
  const base = need("TOKEN_BASE"); // WETH
  const quote = need("TOKEN_QUOTE"); // USDC
  const fee = Number(process.env.UNI_FEE || 500);

  const arb = await ethers.deployContract("FlashArb", [vault, uniRouter, sushiRouter]);
  await arb.waitForDeployment();
  const addr = await arb.getAddress();
  console.log(`FlashArb deployed: ${addr}`);

  const routes = [
    [0, quote, base, true],
    [1, quote, base, false],
    [2, base, quote, true],
    [3, base, quote, false],
  ];
  for (const [id, loanTok, targetTok, uniFirst] of routes) {
    await (await arb.setRoute(id, loanTok, targetTok, fee, uniFirst, true)).wait();
    console.log(`route ${id} set (uniFirst=${uniFirst})`);
  }

  if (process.env.OPERATOR_ADDRESS) {
    await (await arb.setOperator(process.env.OPERATOR_ADDRESS, true)).wait();
    console.log(`operator authorised: ${process.env.OPERATOR_ADDRESS}`);
  }

  // Ownable2Step: the new owner must call acceptOwnership() from their own wallet.
  if (process.env.OWNER_ADDRESS && process.env.OWNER_ADDRESS.toLowerCase() !== deployer.address.toLowerCase()) {
    await (await arb.transferOwnership(process.env.OWNER_ADDRESS)).wait();
    console.log(`ownership transfer started -> ${process.env.OWNER_ADDRESS} (they must call acceptOwnership())`);
  }

  console.log("\nPut this in .env:  ARB_CONTRACT=" + addr);

  if (process.env.VERIFY === "true") {
    await new Promise((r) => setTimeout(r, 30_000)); // let the explorer index the bytecode
    await run("verify:verify", { address: addr, constructorArguments: [vault, uniRouter, sushiRouter] });
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
