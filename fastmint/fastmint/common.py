"""Shared helpers: config loading + RPC clients.

No secrets here. No signing here. RPC URLs come from config/chains.yaml
(public endpoints, no API keys in file).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from web3 import Web3

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = BASE_DIR / "config" / "chains.yaml"

# Chain name aliases -> canonical key in chains.yaml
ALIASES = {
    "eth": "ethereum",
    "ethereum": "ethereum",
    "mainnet": "ethereum",
    "bnb": "bsc",
    "bsc": "bsc",
    "binance": "bsc",
    "base": "base",
    "arb": "arbitrum",
    "arbitrum": "arbitrum",
    "op": "optimism",
    "optimism": "optimism",
    "matic": "polygon",
    "polygon": "polygon",
    "pol": "polygon",
    "robinhood": "robinhood",
    "hood": "robinhood",
    "litvm": "litvm",
    "liteforge": "litvm",
}


def load_config(path: str | Path = DEFAULT_CONFIG) -> dict:
    path = Path(path).expanduser()
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict) or "chains" not in cfg:
        raise ValueError(f"config tidak valid: {path}")
    return cfg


def canonical_chain(name: str) -> str:
    key = ALIASES.get(name.strip().lower())
    if key is None:
        raise ValueError(f"chain tidak dikenal: {name!r}")
    return key


def get_chain(cfg: dict, name: str) -> dict:
    key = canonical_chain(name)
    chain = cfg["chains"].get(key)
    if chain is None:
        raise ValueError(f"chain {key!r} tidak ada di config")
    return dict(chain, _key=key)


def connect_chain(chain: dict, timeout: int = 20) -> Web3:
    """Try RPCs in order, return first Web3 whose chain_id matches config.

    Raises RuntimeError if none work — fail-closed, never guess a chain.
    """
    expected = chain.get("chain_id")
    rpcs = chain.get("rpc") or []
    errors: list[str] = []
    for url in rpcs:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": timeout}))
            if not w3.is_connected():
                errors.append(f"{url}: not connected")
                continue
            got = w3.eth.chain_id
            if expected is not None and got != expected:
                errors.append(f"{url}: chain_id {got} != expected {expected}")
                continue
            return w3
        except Exception as e:  # noqa: BLE001 — collect and try next RPC
            errors.append(f"{url}: {type(e).__name__}: {str(e)[:120]}")
    raise RuntimeError(
        f"tidak bisa konek ke chain {chain.get('display', chain.get('_key'))}: "
        + "; ".join(errors)
    )


def is_valid_address(addr: str) -> bool:
    return isinstance(addr, str) and Web3.is_address(addr)


def checksum(addr: str) -> str:
    return Web3.to_checksum_address(addr)


def wei_to_native(wei: int, decimals: int = 18) -> float:
    return wei / (10 ** decimals)


def gwei_to_wei(gwei: float) -> int:
    return int(gwei * 1_000_000_000)


def agent_wallet(cfg: dict) -> dict:
    return cfg.get("wallets", {}).get("agent_wl_nft", {})


def user_wallet(cfg: dict) -> dict:
    return cfg.get("wallets", {}).get("user", {})


def expand(path: str) -> Path:
    return Path(os.path.expanduser(path))
