"""Fastmint WL NFT v1 — safety-first minting for EVM whitelists.

Modules:
- common: config loading + RPC helpers (no secrets, no signing).
- safety: contract verification + dangerous-function scan (fail-closed).
- gas: live fee strategy, 3 tiers + per-chain max cap.
- executor: build + simulate (eth_call) + broadcast only when live,
  safety PASS, gas under cap, and user confirmed.
- check_mint: CLI that runs safety + gas + simulation and prints PASS/FAIL.
"""

__version__ = "1.0.0"
