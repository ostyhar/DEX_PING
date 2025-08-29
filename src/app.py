#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
dex-ping — SQS -> QuoterV2/V1 dry-run quote -> (DynamoDB swap_events) + Telegram
-------------------------------------------------------------------------------
• Слухає NewDexTokens.fifo (payload від dex-monitor)
• Для кожного пулу робить DRY квоту (без реального свопу) під TARGET_SWAP_AMOUNT
• Якщо квота успішна і amountOut>0 — пише запис у DynamoDB (status=ping_ready) + TG
• Якщо реверт/нуль — status=ping_failed з причиною (+ TG)
"""

from __future__ import annotations
import os
import json
import time
from decimal import Decimal, getcontext
from typing import Any, Dict, Optional, Tuple

import boto3
from botocore.exceptions import ClientError
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()
getcontext().prec = 48

# ---------- Uniswap addresses ----------
FACTORY     = Web3.to_checksum_address("0x1F98431c8aD98523631AE4a59f267346ea31F984")
QUOTER_V2   = Web3.to_checksum_address("0x61fFE014bA17989E743c5F6cB21bF9697530B21e")
QUOTER_V1   = Web3.to_checksum_address("0xb27308f9F90D607463bb33eA1BeBb41C27CE5AB6")
SWAP_ROUTER = Web3.to_checksum_address("0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45")

# sqrt(price) hard limits (із Uniswap v3)
MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342

# ---------- ABIs (мінімально необхідні) ----------
ERC20_ABI = [
    {"name": "decimals", "type": "function", "stateMutability": "view", "inputs": [], "outputs":[{"type":"uint8"}]},
]

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

QUOTER_V1_ABI = [{
  "type":"function","stateMutability":"nonpayable","name":"quoteExactInputSingle",
  "inputs":[
    {"name":"tokenIn","type":"address"},
    {"name":"tokenOut","type":"address"},
    {"name":"fee","type":"uint24"},
    {"name":"amountIn","type":"uint256"},
    {"name":"sqrtPriceLimitX96","type":"uint160"}
  ],
  "outputs":[{"name":"amountOut","type":"uint256"}]
}]

FACTORY_ABI = [{"name":"getPool","type":"function","stateMutability":"view",
                "inputs":[{"type":"address"},{"type":"address"},{"type":"uint24"}],
                "outputs":[{"type":"address"}]}]

# ---------- ENV ----------
RPC_URL   = os.getenv("RPC_URL","").strip()
DYNAMO_TABLE = os.getenv("LOCK_TABLE","swap_events").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN","").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID","").strip()
TELEGRAM_DISABLE_WEB_PAGE_PREVIEW = os.getenv("TELEGRAM_DISABLE_WEB_PAGE_PREVIEW","1") not in ("0","false","False")

# Скільки котирувати (людські одиниці квот-токена, напр. 1.0 USDT або 0.2 WETH)
TARGET_SWAP_AMOUNT = Decimal(os.getenv("TARGET_SWAP_AMOUNT","0") or "0")
# Якщо базова квота впала — зробити пробу на PROBE_BPS від суми (напр. 500 = 5%)
PROBE_BPS = int(os.getenv("PROBE_BPS","500") or "0")

# ---------- Clients ----------
w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 15}))
if not w3.is_connected():
    raise RuntimeError("Web3 connection failed. Check RPC_URL")

quoter_v2 = w3.eth.contract(address=QUOTER_V2, abi=QUOTER_V2_ABI)
quoter_v1 = w3.eth.contract(address=QUOTER_V1, abi=QUOTER_V1_ABI)
factory    = w3.eth.contract(address=FACTORY,  abi=FACTORY_ABI)

dynamodb = boto3.resource("dynamodb")
lock_table = dynamodb.Table(DYNAMO_TABLE)

# ---------- Helpers ----------
def erc20(address: str):
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=ERC20_ABI)

def get_decimals(addr: str) -> int:
    try:
        return int(erc20(addr).functions.decimals().call())
    except Exception:
        return 18

def send_telegram(html: str) -> None:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    import urllib.request, json as _json
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": html,
        "parse_mode": "HTML",
        "disable_web_page_preview": TELEGRAM_DISABLE_WEB_PAGE_PREVIEW,
    }
    req = urllib.request.Request(url, data=_json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception:
        pass

def human_amount(x_wei: int, decimals: int, places: str="1.000000") -> Decimal:
    return (Decimal(x_wei) / (Decimal(10) ** Decimal(decimals))).quantize(Decimal(places))

def price_str(amount_in_wei: int, in_dec: int, amount_out_wei: int, out_dec: int) -> str:
    try:
        ain = Decimal(amount_in_wei)  / (Decimal(10) ** in_dec)
        aout= Decimal(amount_out_wei) / (Decimal(10) ** out_dec)
        if aout > 0:
            px = ain / aout
            return f"{px:.8f}"
    except Exception:
        pass
    return "n/a"

def format_ok_html(ev: dict, amount_in_h: Decimal, out_h: Decimal, px: str) -> str:
    name = ev.get("name") or ev.get("token") or "?"
    fee  = ev.get("fee")
    pool = ev.get("pool")
    parts = [
        f"✅ <b>DEX Ping</b> • {name}",
        f"💱 fee: <code>{fee}</code>",
        f"📥 in: <code>{amount_in_h}</code> ➜ 📦 out: <code>{out_h}</code>",
        f"💵 px: <code>{px}</code>",
    ]
    if pool:
        parts.append(f"🔗 <a href=\"https://etherscan.io/address/{pool}\">pool</a>")
    return "\n".join(parts)

def format_fail_html(ev: dict, err: str) -> str:
    name = ev.get("name") or ev.get("token") or "?"
    fee  = ev.get("fee")
    pool = ev.get("pool")
    parts = [
        f"❌ <b>DEX Ping failed</b> • {name}",
        f"💱 fee: <code>{fee}</code>",
        f"⚠️ {err}",
    ]
    if pool:
        parts.append(f"🔗 <a href=\"https://etherscan.io/address/{pool}\">pool</a>")
    return "\n".join(parts)

def ddb_put_status(idem: str, status: str, payload: dict) -> None:
    """
    Пише запис у swap_events з простим дедупом: перший запис перемагає.
    Якщо елемент із таким `id` вже існує — ігноруємо повтор (ретраї SQS тощо).
    ВАЖЛИВО: ключ таблиці має бути `id` (partition key).
    """
    _id = idem or f"no_idem_{int(time.time())}"
    item = {
        "id": _id,
        "status": status,
        "ts": int(time.time()),
        **payload,
    }
    try:
        lock_table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(#id)",
            ExpressionAttributeNames={"#id": "id"},
        )
        print(f"[DDB] put {status} id={_id} OK")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            print(f"[DDB] duplicate, skip id={_id}")
        else:
            raise

def get_pool_and_direction(token_in: str, token_out: str, fee: int) -> Tuple[str, bool, int]:
    """
    Повертає (pool, oneForZero, sqrt_limit)
    • oneForZero=True: продаємо token1 за token0  -> sqrt_limit = MAX_SQRT_RATIO-1
    • oneForZero=False: продаємо token0 за token1 -> sqrt_limit = MIN_SQRT_RATIO+1
    """
    t_in  = Web3.to_checksum_address(token_in)
    t_out = Web3.to_checksum_address(token_out)
    pool  = factory.functions.getPool(t_in, t_out, int(fee)).call()
    if int(pool, 16) == 0:
        raise RuntimeError("No pool for (tokenIn, tokenOut, fee)")

    # Uniswap v3 впорядковує адреси (token0 < token1)
    t0 = min(t_in, t_out)
    t1 = max(t_in, t_out)
    one_for_zero = (t_in == t1 and t_out == t0)
    sqrt_limit = (MAX_SQRT_RATIO - 1) if one_for_zero else (MIN_SQRT_RATIO + 1)
    return pool, one_for_zero, sqrt_limit

def quote_single(token_in: str, token_out: str, fee: int, amount_in: int, sqrt_limit: int) -> Tuple[int, Optional[str]]:
    """
    Повертає (amountOut, err). Спочатку QuoterV2, фолбек — QuoterV1.
    """
    t_in  = Web3.to_checksum_address(token_in)
    t_out = Web3.to_checksum_address(token_out)
    f     = int(fee)
    amt   = int(amount_in)
    try:
        params = (t_in, t_out, f, amt, int(sqrt_limit))
        out, _, _ = quoter_v2.functions.quoteExactInputSingle(params).call()
        return int(out), None
    except Exception as e_v2:
        try:
            out = quoter_v1.functions.quoteExactInputSingle(t_in, t_out, f, amt, int(sqrt_limit)).call()
            return int(out), None
        except Exception as e_v1:
            return 0, f"revert (v2:{str(e_v2)}) (v1:{str(e_v1)})"

# ---------- Core ----------
def handle_ping_event(ev: dict) -> dict:
    # очікуємо payload від dex-monitor
    for k in ("pool","token","quote","fee"):
        if not ev.get(k):
            return {"skipped":"bad_payload"}

    if TARGET_SWAP_AMOUNT <= 0:
        return {"skipped":"target_amount_not_set"}

    # in-amount у wei (за QUOTE-токеном)
    q_dec = get_decimals(ev["quote"])
    amount_in = int((TARGET_SWAP_AMOUNT * (Decimal(10) ** q_dec)).to_integral_value())

    # Визначаємо напрямок і коректний sqrt limit
    try:
        _pool_ok, _dir, sqrt_limit = get_pool_and_direction(ev["quote"], ev["token"], int(ev["fee"]))
    except Exception as e:
        ddb_put_status(ev.get("idempotencyKey","n/a"), "ping_failed", {"pool": ev.get("pool"), "error": str(e)})
        send_telegram(format_fail_html(ev, f"pool/direction: {e}"))
        return {"error": str(e)}

    # Основна квота
    out, err = quote_single(ev["quote"], ev["token"], int(ev["fee"]), amount_in, sqrt_limit)
    used_amount = amount_in
    used_probe  = False

    # Якщо реверт або out==0 — «маленька проба»
    if (out <= 0 or err) and PROBE_BPS > 0:
        probe_amt = max(1, (amount_in * int(PROBE_BPS)) // 10_000)
        out2, err2 = quote_single(ev["quote"], ev["token"], int(ev["fee"]), probe_amt, sqrt_limit)
        if out2 > 0 and not err2:
            out, err = out2, None
            used_amount = probe_amt
            used_probe  = True
        else:
            ddb_put_status(ev.get("idempotencyKey","n/a"), "ping_failed", {
                "pool": ev.get("pool"),
                "token": ev.get("token"),
                "quote": ev.get("quote"),
                "fee": int(ev.get("fee")),
                "error": err2 or err or "quote returned 0",
                "amount_in": str(amount_in),
                "probe_bps": PROBE_BPS
            })
            send_telegram(format_fail_html(ev, err2 or err or "quote=0"))
            return {"error": err2 or err or "quote=0"}

    if out <= 0:
        ddb_put_status(ev.get("idempotencyKey","n/a"), "ping_failed", {
            "pool": ev.get("pool"),
            "token": ev.get("token"),
            "quote": ev.get("quote"),
            "fee": int(ev.get("fee")),
            "error": err or "quote returned 0",
            "amount_in": str(amount_in),
            "probe_used": used_probe
        })
        send_telegram(format_fail_html(ev, err or "quote=0"))
        return {"error": err or "quote=0"}

    # Красиві числа і ціна
    t_dec = get_decimals(ev["token"])
    amount_in_h = human_amount(used_amount, q_dec)
    out_h       = human_amount(out, t_dec)
    px_str      = price_str(used_amount, q_dec, out, t_dec)

    # Зберігаємо в DDB
    ddb_put_status(ev.get("idempotencyKey","n/a"), "ping_ready", {
        "pool": ev.get("pool"),
        "token": ev.get("token"),
        "quote": ev.get("quote"),
        "fee": int(ev.get("fee")),
        "amount_in": str(used_amount),
        "amount_out": str(out),
        "probe_used": used_probe
    })

    # TG
    send_telegram(format_ok_html(ev, amount_in_h, out_h, px_str))
    return {"ok": True, "out": out}

# ---------- Lambda entry ----------
def lambda_handler(event: dict, context: Any=None) -> dict:
    """
    Працює як SQS-трігер:
    event = {"Records":[{"body":"{...payload from dex-monitor...}"} , ... ]}
    """
    results = []
    try:
        recs = event.get("Records") or []
        if not recs and isinstance(event, dict) and "pool" in event:
            # прямий виклик для локального тесту
            return handle_ping_event(event)
        for r in recs:
            body = json.loads(r.get("body") or "{}")
            results.append(handle_ping_event(body))
    except Exception as e:
        results.append({"error": str(e)})
    return {"results": results}

if __name__ == "__main__":
    import sys
    data = {
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
    if data:
        print(lambda_handler(data))
    else:
        print("Provide JSON payload on stdin (single event or SQS Records).")
