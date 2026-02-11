import re
import time
import base64
import subprocess
import hashlib
import json
from typing import Dict, Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from solders.pubkey import Pubkey
import requests  # compat

app = FastAPI(title="Agent Bunkers v1.1")

SOL_ADDR = "B12jrAGAJD12sbLZSREKQbRXz64qDW6Lyj6nT9VdF969"
SOL_RPC = "https://api.mainnet-beta.solana.com"
sol_client = Client(SOL_RPC)

# Temp store: memo → {'memory': b64, 'agent_id': str, 'tier': str, 'timestamp': int, 'expires': int}
pending_bunkers: Dict[str, dict] = {}
LAMPORTS_SOL = 1_000_000_000

# Rate: agent_id → list timestamps 1h
rates: Dict[str, list[int]] = {}

def rate_check(agent_id: str) -> bool:
    now = int(time.time())
    if agent_id not in rates:
        rates[agent_id] = []
    rates[agent_id] = [t for t in rates[agent_id] if now - t < 3600]
    if len(rates[agent_id]) >= 10:
        return False
    rates[agent_id].append(now)
    return True

def cleanup_pending():
    now = int(time.time())
    global pending_bunkers
    pending_bunkers = {k: v for k, v in pending_bunkers.items() if v['expires'] > now}

class BunkerRequest(BaseModel):
    memory: str  # base64(json)
    agent_id: str
    tier: str  # "standard" | "vault"

class ConfirmRequest(BaseModel):
    tx_hash: str

@app.on_event("startup")
async def startup():
    cleanup_pending()

@app.post("/bunker")
def create_bunker(req: BunkerRequest):
    cleanup_pending()
    
    # Sanitize
    agent_id = re.sub(r'[^a-zA-Z0-9-]', '', req.agent_id)[:64]
    if not agent_id or req.tier not in ["standard", "vault"] or not rate_check(agent_id):
        raise HTTPException(400, "Invalid agent/tier/rate")
    
    try:
        decoded = base64.b64decode(req.memory)
        if len(decoded) > 1_000_000:
            raise ValueError("Payload >1MB")
    except:
        raise HTTPException(400, "Invalid base64 memory")
    
    timestamp = int(time.time())
    memo = f"{agent_id}:{req.tier}:{timestamp}"
    amt_sol = 0.01 if req.tier == "standard" else 0.05
    amt_lamports = int(amt_sol * LAMPORTS_SOL)
    
    # Store pending
    pending_bunkers[memo] = {
        'memory': req.memory,
        'agent_id': agent_id,
        'tier': req.tier,
        'timestamp': timestamp,
        'expires': timestamp + 600  # 10min
    }
    
    return {
        "status": "invoice",
        "solscan": f"https://solscan.io/account/{SOL_ADDR}?amount={amt_sol}&memo={memo}",
        "addr": SOL_ADDR,
        "amount_sol": amt_sol,
        "memo": memo,
        "confirm": f"/confirm/{memo}"
    }

@app.post("/confirm/{memo}")
def confirm_tx(memo: str, req: ConfirmRequest):
    cleanup_pending()
    
    bunker = pending_bunkers.get(memo)
    if not bunker:
        raise HTTPException(400, "No pending bunker (expired)")
    
    agent_id = bunker['agent_id']
    tier = bunker['tier']
    timestamp = bunker['timestamp']
    memory_b64 = bunker['memory']
    if not rate_check(agent_id):
        raise HTTPException(429, "Rate limit")
    
    # Full Solana verify
    try:
        sig = req.tx_hash
        tx_resp = sol_client.get_transaction(sig, encoding="base64", max_supported_transaction_version=0)
        if not tx_resp.value:
            raise ValueError("Tx not found")
        
        meta = tx_resp.value.meta
        if not meta or not meta.err is None:
            raise ValueError("Tx failed")
        
        # Memo match in logs
        logs_str = ' '.join(meta.log_messages or [])
        if memo not in logs_str:
            raise ValueError("Memo mismatch")
        
        # Amount to SOL_ADDR (pre/post balance delta)
        accounts = tx_resp.value.transaction.transaction.message.account_keys
        addr_idx = accounts.index(Pubkey.from_string(SOL_ADDR))
        pre_bal = meta.pre_balances[addr_idx]
        post_bal = meta.post_balances[addr_idx]
        delta = (post_bal - pre_bal) / LAMPORTS_SOL
        amt_req = 0.01 if tier == 'standard' else 0.05
        if delta < amt_req:
            raise ValueError(f"Amount low: {delta} < {amt_req}")
        
    except Exception as e:
        raise HTTPException(400, f"Tx invalid: {str(e)[:100]}")
    
    # Store to vault repo
    ts_str = time.strftime('%Y-%m-%d_%H%M%S', time.localtime(timestamp))
    path = f"{agent_id}/{ts_str}.json"
    
    cmd = [
        'gh', 'api',
        '-X', 'PUT',
        f'-H', 'Accept: application/vnd.github.v3+json',
        f'repos/battlecards/neptune-bunker-vault/contents/{path}',
        '-f', f'message=Bunker {tier}: {agent_id}',
        '-f', f'content={memory_b64}'
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, )
    if result.returncode != 0:
        raise HTTPException(500, f"Commit fail: {result.stderr[:100]}")
    
    del pending_bunkers[memo]
    
    return {
        "status": "bunkered",
        "path": path,
        "tx": req.tx_hash,
        "retrieve": f"/retrieve/{agent_id}/{ts_str}"
    }

@app.get("/retrieve/{agent_id}/{filename}")
def retrieve(agent_id: str, filename: str):
    agent_id = re.sub(r'[^a-zA-Z0-9-]', '', agent_id)[:64]
    path = f"{agent_id}/{filename}"
    
    cmd = [
        'gh', 'api',
        f'repos/battlecards/neptune-bunker-vault/contents/{path}'
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise HTTPException(404, "Not found")
    
    data = json.loads(result.stdout)
    content_b64 = data['content']
    return {"memory": content_b64}  # base64 for agent decode

@app.get("/")
def root():
    cleanup_pending()
    return {
        "agent_bunkers": "v1.1 live",
        "url": "http://35.184.9.234:8000",
        "addr": SOL_ADDR,
        "docs": "/README"  # relative?
    }

@app.get("/health")
def health():
    return {"status": "ok"}