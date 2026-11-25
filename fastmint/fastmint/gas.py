"""Gas strategy — baca fee LIVE dari RPC, 3 tier, + max cap per chain.

Rumus (EIP-1559):
    maxFeePerGas        = baseFee * base_fee_multiplier + maxPriorityFeePerGas
    maxPriorityFeePerGas= persentil reward dari eth_feeHistory (atau
                          eth_maxPriorityFeePerGas sebagai fallback),
                          + priority_boost_gwei dari tier.
    total estimasi      = maxFeePerGas * gas_limit

Chain non-EIP-1559 (BSC): pakai gasPrice legacy = persentil reward
feeHistory (atau eth_gasPrice), maxFee diisi gasPrice itu.

Aturan keras: kalau estimasi total > max_total_fee_native di config
-> over_cap = True -> JANGAN kirim. Laporkan dan tunggu.
Tidak ada angka gas yang di-hardcode sebagai "harga live" — semua dibaca
saat fungsi ini dipanggil.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from web3 import Web3

from .common import connect_chain, get_chain, load_config, wei_to_native

TIERS = ("hemat", "seimbang", "cepat")


@dataclass
class GasResult:
    tier: str
    chain: str
    base_fee_wei: int
    max_priority_fee_wei: int
    max_fee_wei: int
    gas_price_wei: Optional[int]  # legacy, hanya untuk chain non-EIP1559
    gas_limit: int
    estimated_total_wei: int
    estimated_total_native: float
    cap_native: Optional[float]
    under_cap: bool
    native_token: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "chain": self.chain,
            "base_fee_gwei": self.base_fee_wei / 1e9,
            "max_priority_fee_gwei": self.max_priority_fee_wei / 1e9,
            "max_fee_gwei": self.max_fee_wei / 1e9,
            "gas_price_gwei": (self.gas_price_wei / 1e9) if self.gas_price_wei else None,
            "gas_limit": self.gas_limit,
            "estimated_total_native": self.estimated_total_native,
            "cap_native": self.cap_native,
            "under_cap": self.under_cap,
            "native_token": self.native_token,
            "detail": self.detail,
        }


def _percentile(values: list[int], pct: int) -> int:
    """Persentil reward feeHistory (values sudah urut naik per blok)."""
    if not values:
        return 0
    vals = sorted(values)
    idx = min(len(vals) - 1, max(0, int(len(vals) * pct / 100)))
    return vals[idx]


def get_fee_data(
    chain_name: str,
    tier: str = "seimbang",
    gas_limit: Optional[int] = None,
    config_path: Optional[str] = None,
    w3: Optional[Web3] = None,
) -> GasResult:
    if tier not in TIERS:
        raise ValueError(f"tier tidak dikenal: {tier!r} (pilih: {TIERS})")
    cfg = load_config(config_path) if config_path else load_config()
    chain = get_chain(cfg, chain_name)
    gas_cfg = chain.get("gas") or {}
    tier_cfg = gas_cfg.get(tier) or {}
    limit = gas_limit or gas_cfg.get("default_gas_limit") or 200000
    cap = gas_cfg.get("max_total_fee_native")
    native = chain.get("native_token", "?")

    if w3 is None:
        w3 = connect_chain(chain)

    base_fee = 0
    priority = 0
    gas_price = None
    notes: list[str] = []

    # 1. feeHistory (4 blok terakhir) — sumber utama
    try:
        fh = w3.eth.fee_history(4, "latest", [10, 50, 90])
        base_fees = fh.get("baseFeePerGas") or []
        if base_fees:
            base_fee = int(base_fees[-1])
        rewards = fh.get("reward") or []
        pct = tier_cfg.get("priority_percentile", 50)
        per_block = []
        for block_rewards in rewards:
            if block_rewards:
                per_block.append(_percentile([int(x) for x in block_rewards], pct))
        if per_block:
            priority = max(per_block)
        notes.append("feeHistory ok")
    except Exception as e:  # noqa: BLE001
        notes.append(f"feeHistory gagal ({type(e).__name__}), pakai fallback")

    # 2. Fallback priority fee
    if priority == 0:
        try:
            priority = int(w3.eth.max_priority_fee)
            notes.append("maxPriorityFeePerGas ok")
        except Exception:
            pass
    # 3. Fallback gasPrice (wajib untuk chain legacy)
    try:
        gas_price = int(w3.eth.gas_price)
    except Exception:
        gas_price = None
    if base_fee == 0 and gas_price:
        base_fee = gas_price
        notes.append("baseFee dari gasPrice (chain legacy / fallback)")

    boost_wei = int((tier_cfg.get("priority_boost_gwei") or 0) * 1_000_000_000)
    priority = priority + boost_wei
    mult = float(tier_cfg.get("base_fee_multiplier") or 1.0)

    if chain.get("eip1559", True):
        max_fee = int(base_fee * mult) + priority
        effective_price = max_fee
    else:
        # Legacy: harga = max(gasPrice, persentil reward) — feeHistory reward
        # BSC mengembalikan reward sebagai gasPrice-ish; pakai yang terbesar.
        effective_price = max(gas_price or 0, priority, int(base_fee * mult) if base_fee else 0)
        max_fee = effective_price
        priority = 0  # tidak ada konsep priority fee di legacy

    total_wei = effective_price * int(limit)
    total_native = wei_to_native(total_wei, chain.get("native_decimals", 18))
    under_cap = True if cap is None else total_native <= float(cap)
    detail = "; ".join(notes)
    if cap is None:
        detail += "; TIDAK ADA CAP di config — tentukan cap dulu sebelum live"
        under_cap = False  # fail-closed: tanpa cap, jangan kirim live

    return GasResult(
        tier=tier,
        chain=chain.get("_key", chain_name),
        base_fee_wei=int(base_fee),
        max_priority_fee_wei=int(priority),
        max_fee_wei=int(max_fee),
        gas_price_wei=int(gas_price) if gas_price else None,
        gas_limit=int(limit),
        estimated_total_wei=int(total_wei),
        estimated_total_native=float(total_native),
        cap_native=float(cap) if cap is not None else None,
        under_cap=bool(under_cap),
        native_token=native,
        detail=detail,
    )
