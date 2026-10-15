require("@nomicfoundation/hardhat-toolbox");
require("dotenv").config();

const {
  DEPLOYER_PRIVATE_KEY,
  ARBITRUM_RPC_URL = "",
  BASE_RPC_URL = "",
  ARBISCAN_API_KEY = "",
  BASESCAN_API_KEY = "",
} = process.env;

// Only attach a key when one is configured, so `hardhat compile` works with an empty .env
const accounts = DEPLOYER_PRIVATE_KEY ? [DEPLOYER_PRIVATE_KEY] : [];

/** @type import('hardhat/config').HardhatUserConfig */
module.exports = {
  solidity: {
    version: "0.8.24",
    settings: { optimizer: { enabled: true, runs: 200 } },
  },
  networks: {
    arbitrum: { url: ARBITRUM_RPC_URL, chainId: 42161, accounts },
    base: { url: BASE_RPC_URL, chainId: 8453, accounts },
  },
  etherscan: {
    apiKey: { arbitrumOne: ARBISCAN_API_KEY, base: BASESCAN_API_KEY },
  },
};
