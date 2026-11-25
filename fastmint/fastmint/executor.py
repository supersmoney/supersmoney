"""Executor fastmint — dry-run default, broadcast hanya kalau live.

Alur wajib (tidak bisa di-skip):
  1. Safety PASS (lihat safety.py) DAN tidak ada blocking warning.
  2. Gas di bawah cap per chain (lihat gas.py).
  3. Simulasi eth_call + eth_estimateGas OK (tidak revert).
  4. User confirm (ringkasan 1 baris) sebelum signing PERTAMA tiap sesi.
  5. Baru broadcast — dan hanya di mode auto (wallet agent, via keystore).
     Mode approve TIDAK PERNAH sign dengan wallet user; executor hanya
     mengeluarkan transaksi yang siap di-approve user di Phantom/browser.

Aturan secret: password keystore dibaca dari file, private key hasil
decrypt TIDAK PERNAH di-print / di-log / ditulis ke file lain. Yang
boleh tampil hanya alamat wallet.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from eth_account import Account
from web3 import Web3

from .common import (
    agent_wallet,
    checksum,
    connect_chain,
    expand,
    get_chain,
    load_config,
    user_wallet,
)
from .gas import get_fee_data
from .safety import check_contract
from .website import check_website

# Kunci view yang membawa harga mint per unit (dalam wei). Urutan = prioritas.
PRICE_KEYS = (
    "mintPrice", "price", "cost", "publicSalePrice", "whitelistPrice",
    "allowlistPrice", "auctionPrice",
)
# Kunci view yang membawa batas mint per wallet.
MAX_PER_WALLET_KEYS = (
    "maxPerWallet", "maxMintPerWallet", "maxPerAddressDuringMint",
    "maxPerAddress", "maxBatchSize",
)


def onchain_unit_price(mint_params: dict) -> tuple[Optional[int], str]:
    """Ambil harga mint per unit (wei) dari mint params on-chain.

    Return (harga_wei_atau_None, sumber). Explicit key didahulukan.
    Fallback: saleConfig tuple gaya Azuki — index 2 adalah mintPrice
    (uint128) di layout (auctionStartTime, auctionEndTime, mintPrice, ...).
    Kalau tidak ada yang unambiguous -> (None, "") dan pemanggil hanya
    memberi WARN, bukan memblokir (simulasi tetap menangkap underpay).
    """
    for key in PRICE_KEYS:
        val = mint_params.get(key)
        if isinstance(val, int) and val >= 0:
            return val, key
    sale = mint_params.get("saleConfig")
    if isinstance(sale, (list, tuple)) and len(sale) >= 3:
        cand = sale[2]
        if isinstance(cand, int) and 0 < cand <= 100 * 10 ** 18:
            return cand, "saleConfig[2]"
    return None, ""


def check_price_and_quantity(
    mint_params: dict, quantity: int, value_wei: int,
) -> Optional[str]:
    """Return alasan NOT READY kalau harga / quantity tidak cocok, else None.

    Celah yang ditutup (audit v1.1): v1 tidak pernah membandingkan tx
    value dengan harga on-chain — mint yang overpay lolos simulasi dan
    READY, padahal selisihnya hilang ke kontrak. Underpay memang
    ketangkap simulasi (revert), overpay tidak.
    """
    if quantity < 1:
        return f"QUANTITY TIDAK VALID: quantity={quantity} (harus >= 1)."
    if value_wei < 0:
        return f"VALUE TIDAK VALID: value_wei={value_wei} (tidak boleh negatif)."
    for key in MAX_PER_WALLET_KEYS:
        mx = mint_params.get(key)
        if isinstance(mx, int) and mx > 0 and quantity > mx:
            return (
                f"QUANTITY MELEBIHI BATAS: quantity={quantity} > {key}={mx} "
                "on-chain — mint akan revert / dibatasi kontrak."
            )
    price, src = onchain_unit_price(mint_params)
    if price is not None:
        expected = price * quantity
        if value_wei != expected:
            arah = "OVERPAY (kelebihan bayar, selisih hilang ke kontrak)" if value_wei > expected else "UNDERPAY (kurang bayar, mint akan revert)"
            return (
                f"HARGA MINT TIDAK COCOK ({arah}): harga on-chain {src}="
                f"{price} wei x {quantity} = {expected} wei, tapi tx value "
                f"{value_wei} wei. Perbaiki mint_price_wei di project yaml."
            )
    return None

# Kandidat fungsi mint + jumlah argumen yang umum (dipakai kalau project
# tidak menyebutkan nama fungsi secara eksplisit).
DEFAULT_MINT_FN_CANDIDATES = (
    "publicSaleMint", "allowlistMint", "whitelistMint", "presaleMint",
    "mint", "auctionMint", "claim",
)


@dataclass
class PrepResult:
    chain: str
    contract: str
    mode: str
    tier: str
    website_verdict: str = ""
    website: dict = field(default_factory=dict)
    safety_verdict: str = ""
    safety: dict = field(default_factory=dict)
    abi: list = field(default_factory=list)
    gas: dict = field(default_factory=dict)
    simulation: dict = field(default_factory=dict)
    tx: Optional[dict] = None          # unsigned, untuk approve / review
    tx_hash: Optional[str] = None
    receipt: Optional[dict] = None
    balance_before: Optional[int] = None
    balance_after: Optional[int] = None
    ready: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        d = {
            "chain": self.chain,
            "contract": self.contract,
            "mode": self.mode,
            "tier": self.tier,
            "ready": self.ready,
            "reason": self.reason,
            "website_verdict": self.website_verdict,
            "website": self.website,
            "safety_verdict": self.safety_verdict,
            "safety": self.safety,
            "gas": self.gas,
            "simulation": self.simulation,
            "tx": self.tx,
            "tx_hash": self.tx_hash,
            "receipt": self.receipt,
            "balance_before": self.balance_before,
            "balance_after": self.balance_after,
        }
        return d


def load_agent_account(cfg: Optional[dict] = None) -> Account:
    """Decrypt keystore wallet agent. Return LocalAccount.

    Password dibaca dari file, keystore dari path di config. Tidak ada
    secret yang di-print. Alamat hasil decrypt dicocokkan dengan config.
    """
    cfg = cfg or load_config()
    w = agent_wallet(cfg)
    ks_path = expand(w["keystore"])
    pw_path = expand(w["keystore_pass_file"])
    with open(ks_path, "r", encoding="utf-8") as f:
        keystore = json.load(f)
    with open(pw_path, "r", encoding="utf-8") as f:
        password = f.read().strip()
    private_key = Account.decrypt(keystore, password)
    acct = Account.from_key(private_key)
    expected = w.get("address", "")
    if expected and acct.address.lower() != expected.lower():
        raise ValueError(
            f"alamat keystore {acct.address} != alamat config {expected} — STOP"
        )
    return acct


def one_line_summary(
    chain: str, from_addr: str, to_addr: str, asset: str,
    amount_native: float, spender: Optional[str], fee_native: float,
    native_token: str,
) -> str:
    spender_txt = spender or "-"
    return (
        f"{chain} · from {from_addr} · to {to_addr} · {asset} {amount_native} "
        f"· spender {spender_txt} · estimasi fee {fee_native:.6f} {native_token}"
    )


def resolve_mint_call(abi: list, project: dict, quantity: int) -> tuple[str, list]:
    """Tentukan (nama_fungsi, args) untuk mint.

    Prioritas: project['mint_function'] (+ 'mint_args' kalau ada),
    lalu kandidat default yang ada di ABI. Fungsi 0-arg dipanggil tanpa
    argumen, fungsi 1-arg uint dipanggil dengan quantity.
    """
    fns = {a["name"]: a for a in abi if a.get("type") == "function"}
    wanted = project.get("mint_function")
    if wanted:
        if wanted not in fns:
            raise ValueError(f"mint_function {wanted!r} tidak ada di ABI kontrak")
        fn = fns[wanted]
        inputs = fn.get("inputs") or []
        if project.get("mint_args") is not None:
            return wanted, list(project["mint_args"])
        if len(inputs) == 0:
            return wanted, []
        if len(inputs) == 1 and inputs[0]["type"].startswith("uint"):
            return wanted, [quantity]
        raise ValueError(
            f"mint_function {wanted} punya {len(inputs)} argumen — isi mint_args di project yaml"
        )
    for cand in DEFAULT_MINT_FN_CANDIDATES:
        if cand in fns:
            inputs = fns[cand].get("inputs") or []
            if len(inputs) == 0:
                return cand, []
            if len(inputs) == 1 and inputs[0]["type"].startswith("uint"):
                return cand, [quantity]
    raise ValueError("tidak ada fungsi mint yang bisa dipanggil otomatis — isi mint_function + mint_args di project yaml")


def build_mint_tx(
    w3: Web3, chain: dict, contract_addr: str, abi: list,
    mint_fn: str, mint_args: list, value_wei: int, from_addr: str,
    gas: Any,  # GasResult
) -> dict:
    contract = w3.eth.contract(address=checksum(contract_addr), abi=abi)
    fn = getattr(contract.functions, mint_fn)(*mint_args)
    tx: dict[str, Any] = {
        "from": checksum(from_addr),
        "to": checksum(contract_addr),
        "value": int(value_wei),
        "data": fn._encode_transaction_data(),
        "gas": int(gas.gas_limit),
        "chainId": int(chain["chain_id"]),
        # "pending" supaya tx yang masih di mempool ikut dihitung — nonce
        # dari "latest" bisa bentrok kalau ada mint sebelumnya yang belum mined.
        "nonce": int(w3.eth.get_transaction_count(checksum(from_addr), "pending")),
    }
    if chain.get("eip1559", True):
        tx["maxFeePerGas"] = int(gas.max_fee_wei)
        tx["maxPriorityFeePerGas"] = int(gas.max_priority_fee_wei)
        tx["type"] = 2
    else:
        tx["gasPrice"] = int(gas.max_fee_wei)
    return tx


def simulate(w3: Web3, tx: dict) -> dict:
    """eth_call + eth_estimateGas. Return {ok, reason, gas_estimate, return_data}."""
    call_tx = {k: tx[k] for k in ("from", "to", "value", "data", "gas") if k in tx}
    try:
        ret = w3.eth.call(call_tx)
        call_ok, call_reason = True, f"eth_call OK, return {ret.hex()[:66]}"
    except Exception as e:  # noqa: BLE001 — revert adalah hasil, bukan crash
        return {
            "ok": False,
            "reason": f"eth_call REVERT/GAGAL: {type(e).__name__}: {str(e)[:300]}",
            "gas_estimate": None,
            "return_data": None,
        }
    try:
        est = int(w3.eth.estimate_gas(call_tx))
        gas_note = f"estimate_gas {est}"
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "reason": f"eth_call OK tapi estimate_gas gagal: {type(e).__name__}: {str(e)[:300]}",
            "gas_estimate": None,
            "return_data": call_reason,
        }
    return {"ok": True, "reason": call_reason + f"; {gas_note}", "gas_estimate": est, "return_data": None}


def prepare(
    chain_name: str,
    contract: str,
    mode: str = "dry-run",
    tier: str = "seimbang",
    quantity: int = 1,
    value_wei: int = 0,
    project: Optional[dict] = None,
    config_path: Optional[str] = None,
) -> PrepResult:
    """Jalankan safety + gas + build + simulasi. TIDAK broadcast, TIDAK sign.

    Mode:
    - dry-run: hanya cek + simulasi (default).
    - approve: seperti dry-run, tapi `from` = wallet user dan tx unsigned
      dikembalikan untuk di-approve user di wallet-nya sendiri.
    - auto: seperti dry-run, tapi `from` = wallet agent (signing hanya
      terjadi di execute_live, bukan di sini).
    """
    project = project or {}
    if mode not in ("dry-run", "approve", "auto"):
        raise ValueError(f"mode tidak dikenal: {mode!r}")
    cfg = load_config(config_path) if config_path else load_config()
    chain = get_chain(cfg, chain_name)
    res = PrepResult(chain=chain["_key"], contract=contract, mode=mode, tier=tier)

    # 0. Website gate anti-phishing — SEBELUM safety kontrak. Fail-closed.
    #    Tanpa project yaml -> SKIP (mint langsung ke kontrak, tanpa website).
    #    Dengan project yaml tapi tanpa official_website -> FAIL.
    web = check_website(project, contract)
    res.website_verdict = web.verdict
    res.website = web.to_dict()
    if web.verdict == "FAIL":
        res.ready = False
        res.reason = "WEBSITE FAIL — jangan mint via website itu. Lihat website.checks."
        return res

    # 1. Safety
    safety = check_contract(chain["_key"], contract, project=project,
                            config_path=config_path)
    res.safety_verdict = safety.verdict
    res.safety = safety.to_dict()
    res.abi = safety.abi or []
    if safety.verdict != "PASS":
        res.ready = False
        res.reason = "SAFETY FAIL — jangan mint. Lihat safety.checks."
        return res
    if safety.blocking_warnings:
        res.ready = False
        res.reason = "SAFETY PASS tapi ada blocking warning: " + "; ".join(safety.blocking_warnings)
        return res

    # 1b. Harga + quantity vs data on-chain (audit v1.1).
    #      v1 tidak membandingkan tx value dengan harga on-chain, jadi
    #      overpay lolos simulasi. Sekarang dicek sebelum gas/build.
    if value_wei == 0 and project.get("mint_price_wei") is not None:
        value_wei = int(project["mint_price_wei"]) * quantity
    pq_reason = check_price_and_quantity(safety.mint_params, quantity, value_wei)
    if pq_reason:
        res.ready = False
        res.reason = pq_reason
        return res
    price, _src = onchain_unit_price(safety.mint_params)
    if price is None:
        res.safety.setdefault("warnings", []).append(
            "harga_mint: harga on-chain tidak terbaca — pastikan "
            "mint_price_wei di project yaml benar (overpay tidak ketahuan)."
        )

    # 2. Gas
    w3 = connect_chain(chain)
    gas = get_fee_data(chain["_key"], tier=tier, config_path=config_path, w3=w3)
    res.gas = gas.to_dict()
    if not gas.under_cap:
        res.ready = False
        cap = gas.cap_native
        res.reason = (
            f"GAS DI ATAS CAP: estimasi {gas.estimated_total_native:.6f} {gas.native_token} "
            f"> cap {cap} {gas.native_token} — JANGAN kirim, tunggu gas turun."
        )
        return res

    # 3. From address per mode
    if mode == "auto":
        from_addr = agent_wallet(cfg)["address"]
    elif mode == "approve":
        from_addr = user_wallet(cfg)["address"]
    else:
        from_addr = agent_wallet(cfg)["address"]  # dry-run pakai alamat agent sebagai simulasi
    # (value_wei sudah dihitung dari mint_price_wei di atas, sebelum cek harga.)

    # 4. Build + simulate
    try:
        mint_fn, mint_args = resolve_mint_call(safety.abi or [], project, quantity)
        tx = build_mint_tx(w3, chain, checksum(contract), safety.abi or [],
                           mint_fn, mint_args, value_wei, from_addr, gas)
        res.tx = {k: (v.hex() if isinstance(v, bytes) else v) for k, v in tx.items()}
    except Exception as e:  # noqa: BLE001
        res.ready = False
        res.reason = f"BUILD TX GAGAL: {type(e).__name__}: {str(e)[:300]}"
        return res

    sim = simulate(w3, tx)
    res.simulation = sim
    if not sim["ok"]:
        res.ready = False
        res.reason = "SIMULASI GAGAL (revert) — jangan broadcast. " + sim["reason"]
        return res

    # Gas estimate hasil simulasi dipakai untuk perkiraan biaya final
    res.ready = True
    res.reason = (
        f"READY (dry-run): safety PASS, gas di bawah cap, simulasi OK "
        f"(mint {mint_fn}{tuple(mint_args)}, value {value_wei} wei)."
    )
    return res


def execute_live(
    prep: PrepResult,
    confirm_fn: Optional[Callable[[str], bool]] = None,
    config_path: Optional[str] = None,
    wait_timeout: int = 180,
) -> PrepResult:
    """Broadcast SATU mint yang sudah READY. Hanya mode auto.

    Syarat keras: prep.ready True, safety PASS, gas under cap, dan
    confirm_fn(summary) True. Mode approve/dry-run -> ditolak: wallet
    user tidak pernah di-sign oleh sistem ini.
    """
    if prep.mode != "auto":
        raise ValueError(
            f"execute_live hanya untuk mode auto (wallet agent). Mode {prep.mode!r} "
            "tidak bisa di-broadcast oleh sistem — approve manual oleh user."
        )
    if not prep.ready or prep.tx is None:
        raise ValueError(f"prep tidak READY, tidak boleh live: {prep.reason}")
    cfg = load_config(config_path) if config_path else load_config()
    chain = get_chain(cfg, prep.chain)
    w3 = connect_chain(chain)

    # --- Re-check sesaat sebelum signing (audit v1.1, TOCTOU) ---
    # Keadaan on-chain bisa berubah antara prepare() dan live (sale habis,
    # harga berubah, gas naik, nonce maju). v1 langsung sign tx lama —
    # kalau sale sudah habis, tx tetap terkirim lalu revert on-chain
    # (gas hangus). Sekarang semua dicek ulang; gagal -> batal, tanpa broadcast.
    # 1) Gas segar + cek cap lagi.
    fresh_gas = get_fee_data(prep.chain, tier=prep.tier, config_path=config_path, w3=w3)
    prep.gas = fresh_gas.to_dict()
    if not fresh_gas.under_cap:
        prep.ready = False
        prep.reason = (
            f"LIVE DIBATALKAN: gas naik di atas cap sejak prepare "
            f"(estimasi {fresh_gas.estimated_total_native:.6f} > cap "
            f"{fresh_gas.cap_native} {fresh_gas.native_token}) — tidak ada broadcast."
        )
        return prep
    if chain.get("eip1559", True):
        prep.tx["maxFeePerGas"] = int(fresh_gas.max_fee_wei)
        prep.tx["maxPriorityFeePerGas"] = int(fresh_gas.max_priority_fee_wei)
    else:
        prep.tx["gasPrice"] = int(fresh_gas.max_fee_wei)
    # 2) Nonce segar (pending).
    prep.tx["nonce"] = int(w3.eth.get_transaction_count(checksum(prep.tx["from"]), "pending"))
    # 3) Simulasi ulang dengan tx yang sudah diperbarui.
    sim2 = simulate(w3, prep.tx)
    if not sim2["ok"]:
        prep.ready = False
        prep.reason = "LIVE DIBATALKAN: simulasi ulang gagal — " + sim2["reason"]
        return prep
    # 4) Harga + batas per wallet dibaca ulang (best-effort, ABI dari prepare).
    balance_before: Optional[int] = None
    if prep.abi:
        try:
            contract = w3.eth.contract(address=checksum(prep.contract), abi=prep.abi)
            params: dict = {}
            for _name in ("mintPrice", "price", "cost", "publicSalePrice",
                          "whitelistPrice", "allowlistPrice", "saleConfig",
                          "maxPerWallet", "maxMintPerWallet",
                          "maxPerAddressDuringMint", "maxPerAddress"):
                _fn = getattr(contract.functions, _name, None)
                if _fn is None:
                    continue
                try:
                    params[_name] = _fn().call()
                except Exception:
                    continue
            _qty = 1
            # Quantity dihitung dari value vs harga bila memungkinkan; kalau
            # tidak bisa, cek harga memakai value tx apa adanya.
            _price, _src = onchain_unit_price(params)
            if _price is not None:
                _tx_value = int(prep.tx.get("value") or 0)
                if _tx_value % _price == 0 and _price > 0:
                    _qty = max(1, _tx_value // _price)
                _pq = check_price_and_quantity(params, _qty, _tx_value)
                if _pq:
                    prep.ready = False
                    prep.reason = "LIVE DIBATALKAN: " + _pq
                    return prep
            try:
                balance_before = int(contract.functions.balanceOf(checksum(prep.tx["from"])).call())
            except Exception:
                balance_before = None
        except Exception:
            pass  # best-effort: prepare() sudah cek harga; jangan blokir live
    prep.balance_before = balance_before

    # Ringkasan 1 baris sebelum signing (wajib)
    value_wei = int(prep.tx.get("value") or 0)
    fee_wei = int(prep.tx.get("maxFeePerGas") or prep.tx.get("gasPrice") or 0) * int(prep.tx["gas"])
    summary = one_line_summary(
        chain=prep.chain,
        from_addr=str(prep.tx["from"]),
        to_addr=str(prep.tx["to"]),
        asset=chain.get("native_token", "?"),
        amount_native=value_wei / 10 ** int(chain.get("native_decimals", 18)),
        spender=None,
        fee_native=fee_wei / 10 ** int(chain.get("native_decimals", 18)),
        native_token=chain.get("native_token", "?"),
    )
    if confirm_fn is not None:
        if not confirm_fn(summary):
            prep.ready = False
            prep.reason = "DIBATALKAN user saat konfirmasi — tidak ada broadcast."
            return prep
    else:
        raise ValueError("live butuh confirm_fn (user harus confirm ringkasan 1 baris)")

    # (balance_before sudah dibaca di re-check di atas.)

    acct = load_agent_account(cfg)
    signed = acct.sign_transaction(prep.tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    prep.tx_hash = tx_hash.hex()
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=wait_timeout)
    prep.receipt = {
        "status": int(receipt["status"]),
        "blockNumber": int(receipt["blockNumber"]),
        "gasUsed": int(receipt["gasUsed"]),
        "tx_hash": tx_hash.hex(),
        "explorer": f"{chain.get('explorer','')}/tx/{tx_hash.hex()}",
    }
    if int(receipt["status"]) != 1:
        prep.ready = False
        prep.reason = f"TX MASUK TAPI GAGAL on-chain (status 0): {tx_hash.hex()}"
        return prep
    # Verifikasi on-chain: balanceOf sesudah vs sebelum (audit v1.1 — v1
    # punya verify_mint_onchain tapi tidak pernah memanggilnya di jalur live).
    if prep.abi:
        try:
            ver = verify_mint_onchain(
                w3, prep.contract, prep.abi, str(prep.tx["from"]), prep.balance_before
            )
            prep.balance_after = ver.get("balance_after")
            prep.receipt["mint_verified"] = ver.get("verified")
            prep.receipt["mint_verify_reason"] = ver.get("reason")
            if ver.get("verified") is False:
                prep.ready = False
                prep.reason = (
                    f"TX status 1 tapi balance NFT TIDAK bertambah "
                    f"({ver.get('reason')}) — periksa manual, jangan anggap berhasil."
                )
                return prep
        except Exception:
            pass  # best-effort; receipt status 1 tetap sumber kebenaran utama
    prep.reason = f"LIVE OK: tx {tx_hash.hex()} status 1 di blok {int(receipt['blockNumber'])}"
    return prep


def verify_mint_onchain(
    w3: Web3, contract_addr: str, abi: list, owner_addr: str,
    balance_before: Optional[int],
) -> dict:
    """Verifikasi hasil mint via balanceOf sesudah tx (jangan percaya klaim)."""
    contract = w3.eth.contract(address=checksum(contract_addr), abi=abi)
    try:
        after = int(contract.functions.balanceOf(checksum(owner_addr)).call())
    except Exception as e:  # noqa: BLE001
        return {"verified": False, "reason": f"balanceOf gagal: {type(e).__name__}: {str(e)[:150]}"}
    if balance_before is None:
        return {"verified": None, "balance_after": after,
                "reason": "balance sebelum mint tidak tercatat — bandingkan manual"}
    return {
        "verified": after > balance_before,
        "balance_before": balance_before,
        "balance_after": after,
        "reason": f"balance {balance_before} -> {after}",
    }
