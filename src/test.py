from web3 import Web3
import os
from dotenv import load_dotenv

load_dotenv()

w3 = Web3(Web3.HTTPProvider(os.environ["RPC_URL"]))

# FACTORY = w3.to_checksum_address("0x1F98431c8aD98523631AE4a59f267346ea31F984")
# factory_abi = [{"name":"getPool","type":"function","stateMutability":"view",
#                 "inputs":[{"type":"address"},{"type":"address"},{"type":"uint24"}],
#                 "outputs":[{"type":"address"}]}]
# pool_abi = [
#   {"name":"liquidity","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"uint128"}]},
#   {"name":"slot0","type":"function","stateMutability":"view","inputs":[],"outputs":[
#      {"type":"uint160","name":"sqrtPriceX96"},{"type":"int24","name":"tick"},
#      {"type":"uint16"},{"type":"uint16"},{"type":"uint16"},{"type":"uint8"},{"type":"bool"}]}]

# WETH = w3.to_checksum_address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")
# PHA  = w3.to_checksum_address("0x6c5bA91642F10282b576d91922Ae6448C9d52f4E")
# fee  = 10000

# factory = w3.eth.contract(address=FACTORY, abi=factory_abi)
# pool_addr = factory.functions.getPool(WETH, PHA, fee).call()
# print("pool:", pool_addr)

# pool = w3.eth.contract(address=pool_addr, abi=pool_abi)
# liq = pool.functions.liquidity().call()
# print("liquidity():", liq)

# slot0 = pool.functions.slot0().call()
# print("slot0:", slot0)

QUOTER_V2_ABI = [{
   "type":"function","stateMutability":"view","name":"quoteExactInputSingle",
   "inputs":[{"name":"params","type":"tuple","components":[
       {"name":"tokenIn","type":"address"},
       {"name":"tokenOut","type":"address"},
       {"name":"fee","type":"uint24"},
       {"name":"amountIn","type":"uint256"},
       {"name":"sqrtPriceLimitX96","type":"uint160"}
   ]}],
   "outputs":[
       {"name":"amountOut","type":"uint256"},
       {"name":"sqrtPriceX96After","type":"uint160"},
       {"name":"initializedTicksCrossed","type":"uint32"}
   ]
}]

QUOTER_V2=Web3.to_checksum_address("0x61fFE014bA17989E743c5F6cB21bF9697530B21e")

quoter_v2 = w3.eth.contract(address=QUOTER_V2, abi=QUOTER_V2_ABI)

WETH = Web3.to_checksum_address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")
PHA  = Web3.to_checksum_address("0x6c5bA91642F10282b576d91922Ae6448C9d52f4E")
fee  = 10000

MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342

# визначаємо напрямок для заданого пулу (token0 < token1 лексикографічно)
token0, token1 = PHA, WETH
oneForZero = (WETH == token1 and PHA == token0)  # WETH->PHA у цьому пулі

sqrt_limit = (MAX_SQRT_RATIO - 1) if oneForZero else (MIN_SQRT_RATIO + 1)

# спробуй спочатку маленьку суму, напр. 0.1 ETH
amount_in = Web3.to_wei(0.1, "ether")

params = (WETH, PHA, fee, amount_in, sqrt_limit)
amountOut, _, _ = quoter_v2.functions.quoteExactInputSingle(params).call()
print("amountOut:", amountOut)
