// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Ownable, Ownable2Step} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

/*//////////////////////////////////////////////////////////////
                         CUSTOM ERRORS
//////////////////////////////////////////////////////////////*/

/// @notice Thrown when (swap output - loan principal - fee - gas estimate - minProfit) <= 0.
error ArbitrageNotProfitable();
error NotVault();            // callback not called by the Balancer Vault
error NotOperator();         // caller may not trigger arbitrage
error UnexpectedCallback();  // callback was not initiated by this contract
error BadLoanShape();        // loan arrays / token do not match the route
error RouteInactive();
error ZeroAddress();
error SameToken();

/*//////////////////////////////////////////////////////////////
                         MINIMAL INTERFACES
//////////////////////////////////////////////////////////////*/

interface IFlashLoanRecipient {
    function receiveFlashLoan(
        IERC20[] memory tokens,
        uint256[] memory amounts,
        uint256[] memory feeAmounts,
        bytes memory userData
    ) external;
}

/// @dev Balancer V2 Vault (same address on every chain: 0xBA12222222228d8Ba445958a75a0704d566BF2C8).
interface IBalancerVault {
    function flashLoan(
        IFlashLoanRecipient recipient,
        IERC20[] memory tokens,
        uint256[] memory amounts,
        bytes memory userData
    ) external;
}

/// @dev Uniswap V3 SwapRouter02 (NO deadline field in the struct, unlike the original SwapRouter).
interface ISwapRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }

    function exactInputSingle(ExactInputSingleParams calldata params) external payable returns (uint256 amountOut);
}

/// @dev SushiSwap V2 router (Uniswap-V2-compatible).
interface IUniswapV2Router {
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts);
}

/*//////////////////////////////////////////////////////////////
                           MAIN CONTRACT
//////////////////////////////////////////////////////////////*/

/**
 * @title FlashArb
 * @notice Atomic two-leg spatial arbitrage between Uniswap V3 and SushiSwap (V2),
 *         funded by a 0%-fee Balancer V2 flash loan.
 *
 * Security model
 *  - `owner` (ideally a hardware wallet / multisig) configures routes, operators and withdraws profit.
 *  - `operators` (a hot bot key holding only gas ETH) can ONLY trigger `executeArbitrage`.
 *    A leaked bot key can therefore never move profits out of the contract.
 *  - Swap paths (tokens, fee tier, venue order) live in contract storage (`routes`) and are selected
 *    by id. Callers cannot inject arbitrary tokens/routers/paths.
 *  - Balancer lets ANYONE call `vault.flashLoan(ourAddress, ..., anyData)`. We therefore only accept
 *    a callback that matches a hash stored by `executeArbitrage` in the same transaction.
 *  - The whole trade reverts atomically unless it is strictly profitable after gas.
 */
contract FlashArb is Ownable2Step, ReentrancyGuard, IFlashLoanRecipient {
    using SafeERC20 for IERC20;

    /*──────────────────────────── Storage ────────────────────────────*/

    IBalancerVault public immutable VAULT;
    ISwapRouter public immutable UNI_ROUTER;
    IUniswapV2Router public immutable SUSHI_ROUTER;

    /// @param loanToken   Token borrowed from Balancer and returned at the end (e.g. USDC).
    /// @param targetToken Token bought on leg A and sold on leg B (e.g. WETH).
    /// @param uniFee      Uniswap V3 fee tier in hundredths of a bip (500 = 0.05%, 3000 = 0.3%).
    /// @param uniFirst    true  -> leg A on Uniswap V3 (buy target), leg B on Sushi (sell target)
    ///                    false -> leg A on Sushi (buy target),      leg B on Uniswap V3 (sell target)
    struct Route {
        address loanToken;
        address targetToken;
        uint24 uniFee;
        bool uniFirst;
        bool active;
    }

    /// @dev ABI-encoded into the flash loan `userData`.
    struct ArbParams {
        uint256 routeId;
        uint256 minTargetOut;      // slippage floor for leg A (target tokens received)
        uint256 estimatedGasCost;  // gas estimate expressed in loanToken units (supplied by the bot)
        uint256 minProfit;         // extra margin in loanToken units, on top of gas
    }

    mapping(uint256 => Route) public routes;
    mapping(address => bool) public operators;

    /// @dev keccak256(userData) of the in-flight loan; zero when no loan is in flight.
    bytes32 private _pendingHash;

    /*──────────────────────────── Events ─────────────────────────────*/

    event RouteSet(uint256 indexed routeId, address loanToken, address targetToken, uint24 uniFee, bool uniFirst, bool active);
    event OperatorSet(address indexed operator, bool allowed);
    event ArbitrageExecuted(uint256 indexed routeId, uint256 loanAmount, uint256 grossProfit);
    event Withdrawn(address indexed token, address indexed to, uint256 amount);

    /*─────────────────────────── Constructor ─────────────────────────*/

    constructor(address vault, address uniRouter, address sushiRouter) Ownable(msg.sender) {
        if (vault == address(0) || uniRouter == address(0) || sushiRouter == address(0)) revert ZeroAddress();
        VAULT = IBalancerVault(vault);
        UNI_ROUTER = ISwapRouter(uniRouter);
        SUSHI_ROUTER = IUniswapV2Router(sushiRouter);
    }

    /*──────────────────────────── Admin ──────────────────────────────*/

    function setRoute(uint256 routeId, address loanToken, address targetToken, uint24 uniFee, bool uniFirst, bool active)
        external
        onlyOwner
    {
        if (loanToken == address(0) || targetToken == address(0)) revert ZeroAddress();
        if (loanToken == targetToken) revert SameToken();
        routes[routeId] = Route(loanToken, targetToken, uniFee, uniFirst, active);
        emit RouteSet(routeId, loanToken, targetToken, uniFee, uniFirst, active);
    }

    function setOperator(address operator, bool allowed) external onlyOwner {
        if (operator == address(0)) revert ZeroAddress();
        operators[operator] = allowed;
        emit OperatorSet(operator, allowed);
    }

    /// @notice Harvest profit. Owner only.
    function withdraw(address token, address to, uint256 amount) external onlyOwner {
        if (to == address(0)) revert ZeroAddress();
        IERC20(token).safeTransfer(to, amount);
        emit Withdrawn(token, to, amount);
    }

    /// @notice Harvest the entire balance of `token`. Owner only.
    function withdrawAll(address token, address to) external onlyOwner {
        if (to == address(0)) revert ZeroAddress();
        uint256 bal = IERC20(token).balanceOf(address(this));
        IERC20(token).safeTransfer(to, bal);
        emit Withdrawn(token, to, bal);
    }

    /*────────────────────────── Entry point ──────────────────────────*/

    /**
     * @notice Trigger one arbitrage attempt. Reverts (and costs only gas) if not profitable.
     * @param routeId           Pre-configured route (tokens, fee tier, venue order).
     * @param loanAmount        Amount of `route.loanToken` to borrow from Balancer.
     * @param minTargetOut      Minimum target tokens from leg A (slippage floor; 0 to disable).
     * @param estimatedGasCost  Expected tx cost in loanToken units (L2 execution + L1 data fee).
     * @param minProfit         Required profit margin in loanToken units beyond principal + gas.
     */
    function executeArbitrage(
        uint256 routeId,
        uint256 loanAmount,
        uint256 minTargetOut,
        uint256 estimatedGasCost,
        uint256 minProfit
    ) external nonReentrant {
        if (!operators[msg.sender] && msg.sender != owner()) revert NotOperator();

        Route memory r = routes[routeId];
        if (!r.active) revert RouteInactive();

        bytes memory data = abi.encode(ArbParams(routeId, minTargetOut, estimatedGasCost, minProfit));
        _pendingHash = keccak256(data);

        IERC20[] memory tokens = new IERC20[](1);
        tokens[0] = IERC20(r.loanToken);
        uint256[] memory amounts = new uint256[](1);
        amounts[0] = loanAmount;

        VAULT.flashLoan(this, tokens, amounts, data);

        _pendingHash = bytes32(0); // hygiene: callback already cleared it
    }

    /*────────────────────── Flash loan callback ──────────────────────*/

    /// @inheritdoc IFlashLoanRecipient
    function receiveFlashLoan(
        IERC20[] memory tokens,
        uint256[] memory amounts,
        uint256[] memory feeAmounts,
        bytes memory userData
    ) external override {
        // 1) Only the Balancer Vault may call us...
        if (msg.sender != address(VAULT)) revert NotVault();
        // 2) ...and only as the continuation of OUR executeArbitrage call in this very transaction.
        bytes32 expected = _pendingHash;
        if (expected == bytes32(0) || keccak256(userData) != expected) revert UnexpectedCallback();
        _pendingHash = bytes32(0); // single use: blocks replay / re-entry into the callback

        if (tokens.length != 1 || amounts.length != 1 || feeAmounts.length != 1) revert BadLoanShape();

        ArbParams memory p = abi.decode(userData, (ArbParams));
        Route memory r = routes[p.routeId];
        if (address(tokens[0]) != r.loanToken) revert BadLoanShape();

        uint256 principal = amounts[0];
        uint256 owed = principal + feeAmounts[0]; // fee is 0 today; handled in case governance changes it

        // ── Leg A: loanToken -> targetToken ──────────────────────────
        uint256 targetBefore = IERC20(r.targetToken).balanceOf(address(this));
        if (r.uniFirst) {
            _swapUniV3(r.loanToken, r.targetToken, r.uniFee, principal, p.minTargetOut);
        } else {
            _swapSushi(r.loanToken, r.targetToken, principal, p.minTargetOut);
        }
        // Use balance deltas (not router return values) so pre-existing dust and
        // fee-on-transfer tokens cannot distort accounting.
        uint256 targetReceived = IERC20(r.targetToken).balanceOf(address(this)) - targetBefore;

        // ── Leg B: targetToken -> loanToken ──────────────────────────
        uint256 loanBefore = IERC20(r.loanToken).balanceOf(address(this));
        if (r.uniFirst) {
            _swapSushi(r.targetToken, r.loanToken, targetReceived, 0);
        } else {
            _swapUniV3(r.targetToken, r.loanToken, r.uniFee, targetReceived, 0);
        }
        uint256 out = IERC20(r.loanToken).balanceOf(address(this)) - loanBefore;

        // ── Strict profit gate (atomic: a revert unwinds BOTH swaps and the loan) ──
        // net = out - (principal + fee) - gas - minProfit must be strictly > 0
        if (out <= owed + p.estimatedGasCost + p.minProfit) revert ArbitrageNotProfitable();

        // ── Repay Balancer (transfer, not approve: the Vault checks its balance) ──
        IERC20(r.loanToken).safeTransfer(address(VAULT), owed);

        emit ArbitrageExecuted(p.routeId, principal, out - owed);
    }

    /*────────────────────────── Swap helpers ─────────────────────────*/

    function _swapUniV3(address tokenIn, address tokenOut, uint24 fee, uint256 amountIn, uint256 minOut)
        private
        returns (uint256 amountOut)
    {
        IERC20(tokenIn).forceApprove(address(UNI_ROUTER), amountIn); // exact approval, never infinite
        amountOut = UNI_ROUTER.exactInputSingle(
            ISwapRouter.ExactInputSingleParams({
                tokenIn: tokenIn,
                tokenOut: tokenOut,
                fee: fee,
                recipient: address(this),
                amountIn: amountIn,
                amountOutMinimum: minOut,
                sqrtPriceLimitX96: 0
            })
        );
        IERC20(tokenIn).forceApprove(address(UNI_ROUTER), 0);
    }

    function _swapSushi(address tokenIn, address tokenOut, uint256 amountIn, uint256 minOut)
        private
        returns (uint256 amountOut)
    {
        IERC20(tokenIn).forceApprove(address(SUSHI_ROUTER), amountIn);
        address[] memory path = new address[](2);
        path[0] = tokenIn;
        path[1] = tokenOut;
        uint256[] memory amounts =
            SUSHI_ROUTER.swapExactTokensForTokens(amountIn, minOut, path, address(this), block.timestamp);
        amountOut = amounts[1];
        IERC20(tokenIn).forceApprove(address(SUSHI_ROUTER), 0);
    }
}
