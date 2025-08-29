#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dex-ping — SQS -> QuoterV2 probe -> SQS (+ Telegram)
----------------------------------------------------

Приймає події від dex-monitor (univ3.pool.created), робить квоту exactInputSingle
на суму TARGET_SWAP_AMOUNT (у людських одиницях котирувальника), та якщо пул «готовий»
(квота не реверта і amountOut > 0), публікує подію 'univ3.pool.ping.ok' у SQS
для dex-swapper і надсилає повідомлення в Telegram.

ENV (приклад):
  # RPC
  RPC_URL=https://mainnet.infura.io/v3/<...>

  # Вхід/вихідні черги
  # (цей скрипт тригериться SQS-входом через Lambda;
  #  а у вихід — шле готову подію для dex-swapper)
  SQS_OUT_URL=https://sqs.eu-west-1.amazonaws.com/123/NewDexPings.fifo

  # Квота
  TARGET_SWAP_AMOUNT=100.0       # У ЛЮДСЬКИХ одиницях котирувальника (USDT/USDC/WETH)
  PROBE_PCT_BPS=0                # Напр., 100 = 1%. 0 = вимкнено
  MAX_IMPACT_BPS=0               # Ліміт імпакту між probe та основною сумою. 0 = вимкнено
  MIN_EXPECTED_OUT=0             # Мін. кількість таргет-токенів з квоти. 0 = вимкнено

  # TG
  TELEGRAM_BOT_TOKEN=...
  TELEGRAM_CHAT_ID=...
  TELEGRAM_DISABLE_WEB_PAGE_PREVIEW=1

Примітки:
- Використовуємо quoteSymbol з payload монітора, якщо є — менше RPC, краща консистентність.
- Прокидуємо createdBlockHash у вихідний payload для відсікання реоргів далі по конвеєру.
"""

from __future__ import annotations
import os, json, time
from decimal import Decimal, getcontext
from typing import Any, Dict, Optional, Tuple

import boto3
from botocore.exceptions import ClientError
from web3 import Web3
from web3.types import TxParams
from web3.types import HexBytes
from dotenv import load_dotenv

load_dotenv()

# -------- Precision --------
getcontext().prec = 48

# -------- Uniswap addresses (Ethereum mainnet) --------
QUOTER_V2 = Web3.to_checksum_address("0x61fFE014bA17989E743c5F6cB21bF9697530B21e")

# -------- Minimal ABIs --------
ERC20_DECIMALS_ABI = [{
    "constant": True, "inputs": [], "name": "decimals",
    "outputs": [{"name": "", "type": "uint8"}],
    "stateMutability": "view", "type": "function"
}]

# string/bytes32 symbol fallbacks
ERC20_SYMBOL_ABIS = [
    {"name":"symbol","outputs":[{"type":"string"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"symbol","outputs":[{"type":"bytes32"}],"inputs":[],"stateMutability":"view","type":"function"},
]

# QuoterV2: quoteExactInputSingle
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

# ---------- ENV ----------
RPC_URL = os.getenv("RPC_URL", "").strip()
SQS_OUT_URL = os.getenv("SQS_OUT_URL", "").strip()

TARGET_SWAP_AMOUNT = Decimal(os.getenv("TARGET_SWAP_AMOUNT", "100"))
PROBE_PCT_BPS = int(os.getenv("PROBE_PCT_BPS", "0"))          # 0=off, else e.g. 100=1%
MAX_IMPACT_BPS = int(os.getenv("MAX_IMPACT_BPS", "0"))        # 0=off
MIN_EXPECTED_OUT = Decimal(os.getenv("MIN_EXPECTED_OUT", "0"))# 0=off (у люд. одиницях таргет-токена)

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TG_DISABLE_PREVIEW = os.getenv("TELEGRAM_DISABLE_WEB_PAGE_PREVIEW", "1").lower() not in ("0","false","no")
ETHERSCAN_BASE = os.getenv("ETHERSCAN_BASE", "https://etherscan.io").rstrip("/")
UNISWAP_APP_BASE = os.getenv("UNISWAP_APP_BASE", "https://app.uniswap.org").rstrip("/")

# ---------- Boto ----------
sqs = boto3.client("sqs")

# ---------- Web3 ----------
if not RPC_URL:
    raise RuntimeError("RPC_URL is required")
w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 15}))
if not w3.is_connected():
    raise RuntimeError("Web3 connection failed. Check RPC_URL")

quoter_v2 = w3.eth.contract(address=QUOTER_V2, abi=QUOTER_V2_ABI)

# ---------- Helpers ----------
def escape_html(s: str) -> str:
    return str(s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def erc20(address: str, abi) -> Any:
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)

def get_decimals(addr: str) -> int:
    try:
        return int(erc20(addr, ERC20_DECIMALS_ABI).functions.decimals().call())
    except Exception:
        return 18

_SYMBOL_CACHE: Dict[str,str] = {}
def get_symbol(addr: str) -> str:
    if addr in _SYMBOL_CACHE:
        return _SYMBOL_CACHE[addr]
    for abi in ERC20_SYMBOL_ABIS:
        try:
            c = erc20(addr, [abi])
            val = c.functions.symbol().call()
            if isinstance(val, (bytes, bytearray)):
                try:
                    val = val.rstrip(b"\x00").decode("utf-8")
                except Exception:
                    continue
            s = str(val).strip()
            if s:
                _SYMBOL_CACHE[addr] = s
                return s
        except Exception:
            continue
    _SYMBOL_CACHE[addr] = addr
    return addr

def human_to_wei(x: Decimal, decimals: int) -> int:
    q = (x * (Decimal(10) ** decimals)).to_integral_value()
    return int(q)

def wei_to_human(x_wei: int, decimals: int) -> Decimal:
    return (Decimal(x_wei) / (Decimal(10) ** decimals))

def short_addr(a: str) -> str:
    a = Web3.to_checksum_address(a)
    return a[:6] + "…" + a[-4:]

def send_telegram(html: str, max_tries: int = 3):
    if not (TG_TOKEN and TG_CHAT):
        return
    import urllib.request, urllib.error, json as _json
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT,
        "text": html,
        "parse_mode": "HTML",
        "disable_web_page_preview": TG_DISABLE_PREVIEW
    }
    data = _json.dumps(payload).encode("utf-8")
    for i in range(max_tries):
        try:
            req = urllib.request.Request(url, data=data, headers={"Content-Type":"application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            return
        except urllib.error.HTTPError as e:
            if e.code in (429,500,502,503,504):
                time.sleep(2**i)
                continue
            raise
        except Exception:
            if i < max_tries-1:
                time.sleep(2**i)
                continue
            raise

def quoter_v2_exact_input_single(token_in: str, token_out: str, fee: int, amount_in_wei: int) -> Tuple[int,int,int]:
    params = (
        Web3.to_checksum_address(token_in),
        Web3.to_checksum_address(token_out),
        int(fee),
        int(amount_in_wei),
        0  # sqrtPriceLimitX96
    )
    out, sqrt_after, ticks = quoter_v2.functions.quoteExactInputSingle(params).call()
    return int(out), int(sqrt_after), int(ticks)

def calc_impact_bps(px_small: Decimal, px_big: Decimal) -> int:
    if px_small <= 0:
        return 0
    # impact = (px_big - px_small)/px_small * 10000
    diff = (px_big - px_small) / px_small
    return int((diff * Decimal(10000)).to_integral_value())

# ---------- Core ----------
def format_tg_success(ev: dict,
                      quote_symbol_display: str,
                      amount_in_h: Decimal,
                      amount_out_h: Decimal,
                      px_q_per_t: Decimal,
                      probe_impact_bps: Optional[int]) -> str:
    token_addr = Web3.to_checksum_address(ev["token"])
    pool       = Web3.to_checksum_address(ev["pool"])
    fee        = int(ev["fee"])
    txh        = str(ev.get("createdTx") or "")
    qsym       = escape_html(quote_symbol_display)

    lines = []
    lines.append("✅ <b>Ping OK</b> • Uniswap V3")
    lines.append(f"💱 <b>{short_addr(token_addr)}</b> / <b>{qsym}</b> • fee <code>{fee}</code>")
    lines.append(f"💵 in: <code>{amount_in_h.normalize()}</code> {qsym}")
    lines.append(f"📦 out: <code>{amount_out_h.normalize()}</code> tokens")
    lines.append(f"🔢 px: <code>{px_q_per_t.normalize()}</code> {qsym}/token")
    if probe_impact_bps is not None:
        lines.append(f"📉 impact (probe→full): <code>{probe_impact_bps}</code> bps")
    links = []
    links.append(f'<a href="{ETHERSCAN_BASE}/address/{pool}">pool</a>')
    if txh:
        links.append(f'<a href="{ETHERSCAN_BASE}/tx/{txh}">tx</a>')
    links.append(f'<a href="{UNISWAP_APP_BASE}/explore/pools/ethereum/{pool}">app</a>')
    lines.append("\n🔗 " + " · ".join(links))
    return "\n".join(lines)

def publish_ping_ok_to_sqs(ev: dict,
                           amount_in_wei: int,
                           amount_out_wei: int,
                           price_q_per_t_wei_scaled: int,
                           probe_impact_bps: Optional[int]) -> None:
    if not SQS_OUT_URL:
        print("[WARN] SQS_OUT_URL not set; skipping publish")
        return
    payload = {
        "version": 1,
        "event": "univ3.pool.ping.ok",
        "chainId": int(ev.get("chainId", 1)),
        "pool": ev["pool"],
        "token": ev["token"],
        "quote": ev["quote"],
        # символьний тікер від монітора (якщо був)
        "quoteSymbol": (ev.get("quoteSymbol") or "").strip() or None,
        "fee": int(ev["fee"]),
        "createdBlock": int(ev.get("createdBlock", 0)),
        "createdBlockHash": ev.get("createdBlockHash"),
        "createdTx": ev.get("createdTx"),
        "initialized": bool(ev.get("initialized")),
        # квота
        "amountIn": str(amount_in_wei),
        "amountOut": str(amount_out_wei),
        "priceQperT_scaled": str(price_q_per_t_wei_scaled),  # див. примітку нижче
        # опційно — метрика імпакту
        "probeImpactBps": int(probe_impact_bps) if probe_impact_bps is not None else None,
        # додаткове: корисно для idempotency далі
        "idempotencyKey": ev.get("idempotencyKey"),
    }
    # Group & dedupe: групуємо за пулом (або за токеном — на смак)
    group_id = Web3.to_checksum_address(ev["pool"])
    dedup_id = (ev.get("idempotencyKey")
                or Web3.keccak(text="|".join([
                    str(payload["chainId"]), payload["pool"], payload["token"],
                    payload["quote"], str(payload["fee"]), str(payload["createdTx"] or "")
                ])).hex())
    sqs.send_message(
        QueueUrl=SQS_OUT_URL,
        MessageBody=json.dumps(payload),
        MessageGroupId=group_id,
        MessageDeduplicationId=dedup_id
    )
    print("[SQS OUT] published", dedup_id)

def handle_pool_created(ev: dict) -> dict:
    """
    Обробляє payload від dex-monitor (univ3.pool.created).
    Повертає {"ok": True} якщо квота пройшла фільтри і подію опубліковано.
    """
    # Базова валідація
    if str(ev.get("event")) != "univ3.pool.created":
        return {"skipped": "unknown_event"}
    for k in ("pool","token","quote","fee"):
        if not ev.get(k):
            return {"skipped": f"missing_{k}"}

    token = Web3.to_checksum_address(ev["token"])   # таргет
    quote = Web3.to_checksum_address(ev["quote"])   # котирувальник
    fee   = int(ev["fee"])

    # Символ котирувальника: віддаємо пріоритет полю з монітора
    provided_qsym = (ev.get("quoteSymbol") or "").strip()
    quote_symbol = provided_qsym or get_symbol(quote)

    # Decimals
    q_dec = get_decimals(quote)
    t_dec = get_decimals(token)

    # amountIn (wei)
    amount_in_h = Decimal(TARGET_SWAP_AMOUNT)
    amount_in_wei = human_to_wei(amount_in_h, q_dec)

    # ---- Основна квота ----
    try:
        amount_out_wei, sqrt_after, ticks = quoter_v2_exact_input_single(token, quote, fee, amount_in_wei)
    except Exception as e:
        return {"skipped": f"quoter_revert: {e}"}

    if amount_out_wei <= 0:
        return {"skipped": "zero_amount_out"}

    # Мінімальний out-фільтр (у людських одиницях таргет-токена)
    amount_out_h = wei_to_human(amount_out_wei, t_dec)
    if MIN_EXPECTED_OUT > 0 and amount_out_h < MIN_EXPECTED_OUT:
        return {"skipped": f"min_expected_out_not_met: got {amount_out_h}, need >= {MIN_EXPECTED_OUT}"}

    # Ціна (котирувальник за 1 токен)
    # px = (amountIn / 10^q_dec) / (amountOut / 10^t_dec)
    if amount_out_h == 0:
        return {"skipped": "div_by_zero"}
    px_q_per_t = (amount_in_h / amount_out_h)  # Decimal

    # (опційно) quick-probe для оцінки імпакту
    probe_impact_bps: Optional[int] = None
    if PROBE_PCT_BPS > 0 and MAX_IMPACT_BPS > 0:
        probe_in_h = (amount_in_h * Decimal(PROBE_PCT_BPS) / Decimal(10000))
        probe_in_wei = max(1, human_to_wei(probe_in_h, q_dec))
        try:
            probe_out_wei, _, _ = quoter_v2_exact_input_single(quote, token, fee, probe_in_wei)
            if probe_out_wei > 0:
                probe_out_h = wei_to_human(probe_out_wei, t_dec)
                px_small = (probe_in_h / probe_out_h) if probe_out_h > 0 else Decimal(0)
                px_big   = px_q_per_t
                probe_impact_bps = calc_impact_bps(px_small, px_big)
                if probe_impact_bps > MAX_IMPACT_BPS:
                    return {"skipped": f"impact_too_high: {probe_impact_bps}bps > {MAX_IMPACT_BPS}bps"}
        except Exception:
            # якщо проба ревертнула — ігноруємо пробу, але це тривожний сигнал
            probe_impact_bps = None

    # Телеграм — success
    tg_html = format_tg_success(ev, quote_symbol, amount_in_h, amount_out_h, px_q_per_t, probe_impact_bps)
    try:
        send_telegram(tg_html)
    except Exception as e:
        print("[WARN] telegram send failed:", e)

    # Для зручності даємо ще «масштабовану» ціну цілим числом (щоб уникати float у споживачів):
    # priceQperT_scaled = px * 10^q_dec   (тобто скільки "вей котирувальника" за 1 токен)
    price_q_per_t_scaled = int((px_q_per_t * (Decimal(10) ** q_dec)).to_integral_value())

    # Публікуємо в SQS → dex-swapper
    try:
        publish_ping_ok_to_sqs(ev, amount_in_wei, int(amount_out_wei), price_q_per_t_scaled, probe_impact_bps)
    except Exception as e:
        print("[WARN] sqs publish failed:", e)

    return {"ok": True}

# ---------- Lambda handler ----------
def lambda_handler(event: Optional[Dict[str, Any]] = None, context: Any = None) -> Dict[str, Any]:
    """
    SQS → Lambda batch. Повертаємо "batchItemFailures" для негативних кейсів, щоб SQS міг ретраїти.
    """
    failures = []
    for r in (event or {}).get("Records", []):
        msg_id = r.get("messageId")
        try:
            payload = json.loads(r.get("body") or "{}")
            res = handle_pool_created(payload)
            # якщо ми "skipped" — це не помилка, просто неготово/непотрібно
            if "error" in res:
                failures.append({"itemIdentifier": msg_id})
        except Exception as e:
            print("[ERROR] exception in record:", e)
            failures.append({"itemIdentifier": msg_id})
    if failures:
        return {"batchItemFailures": failures}
    return {}

# ---------- Local debug ----------
if __name__ == "__main__":
    # Простий локальний запуск: підстав свій payload нижче або прочитай із файлу/STDIN
    sample = {
            "version": 1,
            "event": "univ3.pool.created",
            "chainId": 1,
            "pool": "0x7bc5c9dE2DFe90CFE1e01967096915ba8ea1Bc53",
            "token": "0x6c5bA91642F10282b576d91922Ae6448C9d52f4E",
            "quote": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            "fee": 10000,
            "createdBlock": 15561122,
            "createdBlockHash": "0x633f0d257cf64d5831d0b12c6ba66e9a72a754436f651fb2c7e98e670f8e429e",
            "createdTx": "0x5911e2ec786e5cd3d8896b1e1287d04d17666b8273506b3e7363389db64bf6dc",
            "initialized": "true",
            "init": {
                "blockNumber": 15561122,
                "txHash": "0x5911e2ec786e5cd3d8896b1e1287d04d17666b8273506b3e7363389db64bf6dc",
                "sqrtPriceX96": "657192322148935038807894396",
                "tick": -95847
            },
            "idempotencyKey": "0x5bc8ea9ec90e036c8d560fedad680c87927729110779037e320fe3e3756f0388"
        }
    try:
        print(handle_pool_created(sample))
    except Exception as e:
        print("Error:", e)
