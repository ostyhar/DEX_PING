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


_SYMBOL_CACHE: dict[str, str] = {}

_ERC20_SYMBOL_ABIS = [
    {"name":"symbol","outputs":[{"type":"string"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"symbol","outputs":[{"type":"bytes32"}],"inputs":[],"stateMutability":"view","type":"function"},
]

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
SWAP_SQS_FIFO_URL = os.getenv("SWAP_SQS_FIFO_URL","").strip()

# ---------- Clients ----------
w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 15}))
if not w3.is_connected():
    raise RuntimeError("Web3 connection failed. Check RPC_URL")

quoter_v2 = w3.eth.contract(address=QUOTER_V2, abi=QUOTER_V2_ABI)
quoter_v1 = w3.eth.contract(address=QUOTER_V1, abi=QUOTER_V1_ABI)
factory    = w3.eth.contract(address=FACTORY,  abi=FACTORY_ABI)

dynamodb = boto3.resource("dynamodb")
lock_table = dynamodb.Table(DYNAMO_TABLE)
sqs = boto3.client("sqs")
AWS_REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "eu-west-1"
scheduler = boto3.client("scheduler", region_name=AWS_REGION)

# ---------- Helpers ----------
def erc20(address: str):
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=ERC20_ABI)

def get_decimals(addr: str) -> int:
    try:
        return int(erc20(addr).functions.decimals().call())
    except Exception:
        return 18

def get_token_symbol(w3: Web3, addr: str) -> str:
    addr = Web3.to_checksum_address(addr)
    if addr in _SYMBOL_CACHE:
        return _SYMBOL_CACHE[addr]
    for abi in _ERC20_SYMBOL_ABIS:
        try:
            c = w3.eth.contract(address=addr, abi=[abi])
            val = c.functions.symbol().call()
            if isinstance(val, (bytes, bytearray)):
                try:
                    val = val.rstrip(b"\x00").decode("utf-8")
                except Exception:
                    continue
            sym = str(val).strip()
            if sym:
                _SYMBOL_CACHE[addr] = sym
                return sym
        except Exception:
            continue
    # фолбек — обрізана адреса
    short = addr[:6] + "…" + addr[-4:]
    _SYMBOL_CACHE[addr] = short
    return short

def _make_sqs_dedup_id(payload: dict) -> str:
    """
    Стабільний dedup id для FIFO: ураховує ідемпотенсі-ключ монітора, пул, фі і суму in.
    """
    ev = payload.get("ev", {}) or {}
    base = "|".join([
        str(ev.get("idempotencyKey","")),
        "ping_ready",
        str(ev.get("pool","")).lower(),
        str(ev.get("fee","")),
        str(payload.get("amount_in","")),
    ])
    return Web3.keccak(text=base).hex()

def publish_ping_ready_to_sqs(payload: dict) -> None:
    """
    Відправляє повідомлення для dex-swapper про готовність до свопу.
    Очікує, що payload містить:
      ev, token(sym), quote(sym), amount_in, amount_out, amount_in_h, out_h, px_str, probe_used
    """
    if not SWAP_SQS_FIFO_URL:
        print("[SQS] SWAP_SQS_FIFO_URL not set — skip publish")
        return
    ev = payload.get("ev", {}) or {}
    body = {
        "version": 1,
        "event": "dex.ping.ready",
        "source": "dex-ping",
        "chainId": ev.get("chainId", 1),
        "pool": ev.get("pool"),
        "token": ev.get("token"),
        "quote": ev.get("quote"),
        "fee": ev.get("fee"),
        "createdBlock": ev.get("createdBlock"),
        "createdBlockHash": ev.get("createdBlockHash"),
        "createdTx": ev.get("createdTx"),
        "idempotencyKey": ev.get("idempotencyKey"),
        "symbols": {"token": payload.get("token"), "quote": payload.get("quote")},
        "quoteResult": {
            "amountIn": str(payload.get("amount_in")),
            "amountOut": str(payload.get("amount_out")),
            "amountInHuman": str(payload.get("amount_in_h")),
            "amountOutHuman": str(payload.get("out_h")),
            "price": payload.get("px_str"),
            "probeUsed": bool(payload.get("probe_used")),
        },
    }
    dedup_id = _make_sqs_dedup_id(payload)
    group_id = ev.get("pool") or (str(ev.get("token","")) + ":" + str(ev.get("quote","")))
    sqs.send_message(
        QueueUrl=SWAP_SQS_FIFO_URL,
        MessageBody=json.dumps(body),
        MessageGroupId=group_id,
        MessageDeduplicationId=dedup_id,
    )
    print("[SQS] published ping_ready", dedup_id)

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

def format_ok_html(ev: dict, token:str, quote: str, amount_in_h: Decimal, out_h: Decimal, px: str) -> str:
    fee  = ev.get("fee")
    pool = ev.get("pool")
    parts = [
        f"✅ <b>DEX Ping</b> • {token}/{quote}",
        f"💱 fee: <code>{fee}</code>",
        f"📥 in: <code>{amount_in_h}</code> {quote}",
        f"💵 price: <code>{px}</code>",
        f"📦 out: <code>{out_h}</code> {token}",
    ]
    if pool:
        parts.append(f"🔗 <a href=\"https://etherscan.io/address/{pool}\">pool</a>")
    return "\n".join(parts)

def dynamo_db_put_status(idem, status, payload):
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
        "pool": payload.get("ev").get("pool"),
        "token": payload.get("ev").get("token"),
        "quote": payload.get("ev").get("quote"),
        "fee": payload.get("ev").get("fee"),
        "amount_in": payload.get("amount_in"),
        "amount_out": payload.get("amount_out"),
        "probe_used": payload.get("probe_used")
    }
    try:
        lock_table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(#id)",
            ExpressionAttributeNames={"#id": "id"},
        )
        print(f"[DDB] put {status} id={_id} OK")
        if status == "ping_ready":
            send_telegram(
                format_ok_html(
                    payload.get("ev"),
                    payload.get("token"),
                    payload.get("quote"),
                    payload.get("amount_in_h"),
                    payload.get("out_h"),
                    payload.get("px_str")
                )
            )
            delete_schedule_if_present(payload.get("ev"))
            publish_ping_ready_to_sqs(payload)

    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            print(f"[DDB] duplicate, skip id={_id}")
        else:
            raise

def delete_schedule_if_present(ev: dict) -> None:
    """
    Якщо в payload є scheduleName/scheduleGroup — пробуємо видалити шедул,
    щоби зупинити подальші щохвилинні виклики.
    """
    name = ev.get("scheduleName")
    group = ev.get("scheduleGroup")
    if not (name and group):
        return
    try:
        scheduler.delete_schedule(Name=name, GroupName=group)
        print(f"[SCHED] deleted: {group}/{name}")
    except Exception as e:
        # Не валимо пінґ через це — просто лог
        print(f"[SCHED][WARN] delete failed for {group}/{name}: {e}")

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
    q_dec = get_decimals(ev.get("quote"))
    amount_in = int((TARGET_SWAP_AMOUNT * (Decimal(10) ** q_dec)).to_integral_value())
    sym_out = get_token_symbol(w3, ev.get("token"))
    sym_in  = ev.get("quote_symbol") or get_token_symbol(w3, ev.get("quote"))
   
    # Визначаємо напрямок і коректний sqrt limit
    try:
        _pool_ok, _dir, sqrt_limit = get_pool_and_direction(ev["quote"], ev["token"], int(ev["fee"]))
    except Exception as e:
        print(f"=========== ping_failed = Exception: {str(e)}")
        dynamo_db_put_status(
            ev.get("idempotencyKey","n/a"),
            "ping_failed",
            {"ev": ev, "token": sym_out, "quote": sym_in, "error": str(e)}
        )
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
            print(f"=========== ping_failed = реверт або out==0 — «маленька проба»")
            dynamo_db_put_status(ev.get("idempotencyKey","n/a"), "ping_failed", {
                "ev": ev,
                "token": sym_out,
                "quote": sym_in,
                "error": err2 or err or "quote returned 0",
                "amount_in": str(amount_in),
                "probe_bps": PROBE_BPS
            })
            return {"error": err2 or err or "quote=0"}

    if out <= 0:
        print(f"=========== ping_failed = out <= 0")
        dynamo_db_put_status(ev.get("idempotencyKey","n/a"), "ping_failed", {
            "ev": ev,
            "token": sym_out,
            "quote": sym_in,
            "error": err or "quote returned 0",
            "amount_in": str(amount_in),
            "probe_used": used_probe
        })
        return {"error": err or "quote=0"}

    # Красиві числа і ціна
    t_dec = get_decimals(ev.get("token"))
    amount_in_h = human_amount(used_amount, q_dec)
    out_h       = human_amount(out, t_dec)
    px_str      = price_str(used_amount, q_dec, out, t_dec)
    # Зберігаємо в DDB
    try:
        payload = {
            "ev": ev,
            "token": sym_out,
            "quote": sym_in,
            "amount_in": str(used_amount),
            "amount_out": str(out),
            "probe_used": used_probe,
            "amount_in_h": amount_in_h,
            "out_h": out_h,
            "px_str": px_str
        }
        dynamo_db_put_status(ev.get("idempotencyKey","n/a"), "ping_ready", payload)
    except Exception:
        raise
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
            # прямий виклик (EventBridge Scheduler або локальний тест)
            print(f"Event: {event}")
            return handle_ping_event(event)
        for r in recs:
            body = json.loads(r.get("body") or "{}")
            results.append(handle_ping_event(body))
    except Exception as e:
        results.append({"error": str(e)})
    return {"results": results}

if __name__ == "__main__":
    import sys

    # WLFI
    # data = {"version": 1, "event": "univ3.pool.created", "chainId": 1, "pool": "0xCa2e972f081764c30Ae5F012A29D5277EEf33838", "token": "0xdA5e1988097297dCdc1f90D4dFE7909e847CBeF6", "quote": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "fee": 10000, "createdBlock": 23260820, "createdBlockHash": "0x6e6a65e48bc0d51a6024ab4fdd1475f258104e3f5488e933f243ee0db2101d46", "createdTx": "0xa2a3ee3aef9c80c80a3d44b15bfa17330b88a024a37fece70d08ac7bb0dafc2a", "initialized": "true", "init": {"blockNumber": 23260820, "txHash": "0xa2a3ee3aef9c80c80a3d44b15bfa17330b88a024a37fece70d08ac7bb0dafc2a", "sqrtPriceX96": 11204090814846941428091632146247, "tick": 99039}, "quoteSymbol": "WETH", "idempotencyKey": "420cb78c293b37707db3c53a76dceff15d3dd1f1c5153c87b35a83950f0e932b", "createdAt": 1756647534, "scheduleName": "dex-ping-f33838-10000-420cb78c", "scheduleGroup": "dex-tokens"}
    # PHA
    data = {"version": 1, "event": "univ3.pool.created", "chainId": 1, "pool": "0x7bc5c9dE2DFe90CFE1e01967096915ba8ea1Bc53", "token": "0x6c5bA91642F10282b576d91922Ae6448C9d52f4E", "quote": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "fee": 10000, "createdBlock": 15561122, "createdBlockHash": "0x633f0d257cf64d5831d0b12c6ba66e9a72a754436f651fb2c7e98e670f8e429e", "createdTx": "0x5911e2ec786e5cd3d8896b1e1287d04d17666b8273506b3e7363389db64bf6dc", "initialized": "true", "init": {"blockNumber": 15561122, "txHash": "0x5911e2ec786e5cd3d8896b1e1287d04d17666b8273506b3e7363389db64bf6dc", "sqrtPriceX96": 657192322148935038807894396, "tick": -95847}, "quoteSymbol": "WETH", "idempotencyKey": "006a04c25e7d2f556d3bad380e508d3c38d34a7e62000115617e0d91a9404ba0", "createdAt": 1756651129, "scheduleName": "dex-ping-a1Bc53-10000-006a04c2", "scheduleGroup": "dex-tokens"}

    if data:
        print(lambda_handler(data))
    else:
        print("Provide JSON payload on stdin (single event or SQS Records).")
