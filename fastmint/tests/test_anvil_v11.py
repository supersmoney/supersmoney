"""Uji E2E Anvil (lokal, tanpa dana asli) untuk perbaikan audit v1.1.

Bukti yang dicari:
  A. Overpay: tx value 0.05 ETH untuk mint harga 0.01 ETH — simulasi v1
     LULUS (v1 akan bilang READY), tapi cek harga v1.1 menolak OVERPAY.
  B. TOCTOU: prepare READY, lalu supply habis dibeli orang lain SEBELUM
     live — execute_live v1.1 harus BATAL sebelum broadcast (tidak ada
     tx hash, tidak ada gas hangus). v1 akan broadcast lalu revert.
  C. Happy path: execute_live berhasil + balanceOf 0 -> 1 terverifikasi.

Jalankan: ~/workspace/airdrop-agent/.venv/bin/python tests/test_anvil_v11.py
Butuh forge/cast/anvil di ~/.foundry/bin (sudah terinstal di VM ini).
Hanya Anvil lokal + key test Anvil yang sudah publik (0xac09...).
TIDAK ada broadcast mainnet/testnet, TIDAK ada dana asli.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eth_account import Account  # noqa: E402
from web3 import Web3  # noqa: E402

from fastmint.common import ALIASES, load_config  # noqa: E402
from fastmint.executor import (  # noqa: E402
    PrepResult,
    build_mint_tx,
    check_price_and_quantity,
    execute_live,
    simulate,
)
from fastmint.gas import get_fee_data  # noqa: E402

PY = str(Path.home() / "workspace/airdrop-agent/.venv/bin/python")
FOUND = Path.home() / ".foundry" / "bin"
ANVIL_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ANVIL_ACCT = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
AGENT = "0x1111111111111111111111111111111111111111"  # dummy — diganti akun sekali-pakai di main()
PRICE = 10 ** 16  # 0.01 ETH
WORK = Path("/tmp/fastmint-anvil-v11")

CONTRACT_SRC = """\
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;
contract CappedMint {
    mapping(address => uint256) public balanceOf;
    mapping(uint256 => address) public ownerOf;
    mapping(address => uint256) public mintedBy;
    uint256 public totalMinted;
    uint256 public constant PRICE = 0.01 ether;
    uint256 public constant MAX_SUPPLY = 1;
    uint256 public constant MAX_PER_WALLET = 2;
    function price() external pure returns (uint256) { return PRICE; }
    function maxPerWallet() external pure returns (uint256) { return MAX_PER_WALLET; }
    function maxSupply() external pure returns (uint256) { return MAX_SUPPLY; }
    function mint(uint256 qty) external payable {
        require(totalMinted + qty <= MAX_SUPPLY, "sold out");
        require(mintedBy[msg.sender] + qty <= MAX_PER_WALLET, "max per wallet");
        // Pola umum di kontrak nyata: >= tanpa refund — kelebihan bayar
        // HILANG ke kontrak. Inilah celah v1: simulasi LULUS untuk overpay.
        require(msg.value >= PRICE * qty, "wrong price");
        for (uint256 i = 0; i < qty; i++) {
            totalMinted += 1;
            ownerOf[totalMinted] = msg.sender;
        }
        balanceOf[msg.sender] += qty;
        mintedBy[msg.sender] += qty;
    }
}
"""

PASS = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAILURES.append(name)
        print(f"  [GAGAL] {name} — {detail}")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def main() -> int:
    # --- Setup: compile + anvil + config temp ---
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "foundry.toml").write_text(
        '[profile.default]\nsrc = "src"\nout = "out"\n')
    (WORK / "src").mkdir(exist_ok=True)
    (WORK / "src" / "CappedMint.sol").write_text(CONTRACT_SRC)
    r = run([str(FOUND / "forge"), "build"], cwd=WORK)
    assert r.returncode == 0, f"forge build gagal: {r.stdout}\n{r.stderr}"
    art = json.loads((WORK / "out" / "CappedMint.sol" / "CappedMint.json").read_text())
    abi, bytecode = art["abi"], art["bytecode"]["object"]
    print("  forge build OK")

    anvil = subprocess.Popen(
        [str(FOUND / "anvil"), "--port", "8545", "--chain-id", "31337"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        w3 = Web3(Web3.HTTPProvider("http://127.0.0.1:8545",
                                    request_kwargs={"timeout": 10}))
        for _ in range(30):
            if w3.is_connected():
                break
            time.sleep(0.5)
        assert w3.is_connected(), "anvil tidak mau konek"

        # Config temp: chains.yaml + chain anvil.
        import yaml
        cfg = load_config()
        # Keystore sekali-pakai (dibuat di WORK, Anvil lokal saja) supaya
        # uji ini tidak bergantung pada keystore agent yang asli dan tidak
        # membocorkan alamat agent ke repo publik. Akun acak ini yang
        # dipakai sebagai AGENT di semua bagian uji.
        global AGENT
        _test_acct = Account.create()
        _ks = Account.encrypt(_test_acct.key, "test-password-anvil")
        (WORK / "agent-keystore.json").write_text(json.dumps(_ks))
        (WORK / ".keystore-pass").write_text("test-password-anvil")
        AGENT = _test_acct.address
        cfg.setdefault("wallets", {})["agent_wl_nft"] = {
            "address": AGENT,
            "keystore": str(WORK / "agent-keystore.json"),
            "keystore_pass_file": str(WORK / ".keystore-pass"),
            "purpose": "keystore sekali-pakai untuk uji Anvil",
        }
        cfg["chains"]["anvil"] = {
            "display": "Anvil Lokal", "chain_id": 31337, "eip1559": True,
            "rpc": ["http://127.0.0.1:8545"],
            "explorer": "", "explorer_api": "", "sourcify_chain_id": None,
            "native_token": "ETH", "native_decimals": 18,
            "verification": "manual",
            "gas": {
                "hemat": {"base_fee_multiplier": 1.05, "priority_percentile": 10,
                          "priority_boost_gwei": 0},
                "seimbang": {"base_fee_multiplier": 1.15, "priority_percentile": 50,
                             "priority_boost_gwei": 0},
                "cepat": {"base_fee_multiplier": 1.30, "priority_percentile": 90,
                          "priority_boost_gwei": 0},
                "default_gas_limit": 200000,
                "max_total_fee_native": 1.0,
            },
        }
        cfg_path = str(WORK / "chains-anvil.yaml")
        with open(cfg_path, "w") as f:
            yaml.safe_dump(cfg, f)
        ALIASES["anvil"] = "anvil"

        deployer = w3.eth.account.from_key(ANVIL_KEY)

        def send_raw(tx: dict) -> str:
            tx.setdefault("nonce", w3.eth.get_transaction_count(deployer.address, "pending"))
            tx.setdefault("gas", 3_000_000)
            tx.setdefault("maxFeePerGas", w3.eth.gas_price * 2)
            tx.setdefault("maxPriorityFeePerGas", 10 ** 9)
            tx.setdefault("chainId", 31337)
            signed = deployer.sign_transaction(tx)
            h = w3.eth.send_raw_transaction(signed.raw_transaction)
            rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=60)
            assert rcpt["status"] == 1, f"tx gagal: {h.hex()}"
            return h.hex()

        def deploy() -> str:
            contract = w3.eth.contract(abi=abi, bytecode=bytecode)
            tx = contract.constructor().build_transaction({"from": ANVIL_ACCT})
            h = send_raw(tx)
            rcpt = w3.eth.get_transaction_receipt(h)
            return rcpt["contractAddress"]

        def fund_agent() -> None:
            send_raw({"from": ANVIL_ACCT, "to": AGENT,
                      "value": 10 ** 18, "gas": 21000})

        def make_prep(contract_addr: str, value_wei: int, qty: int = 1) -> PrepResult:
            chain = dict(cfg["chains"]["anvil"], _key="anvil")
            gas = get_fee_data("anvil", tier="seimbang", config_path=cfg_path, w3=w3)
            tx = build_mint_tx(w3, chain, contract_addr, abi, "mint", [qty],
                               value_wei, AGENT, gas)
            tx = {k: (v.hex() if isinstance(v, bytes) else v) for k, v in tx.items()}
            return PrepResult(chain="anvil", contract=contract_addr, mode="auto",
                              tier="seimbang", website_verdict="SKIP",
                              safety_verdict="PASS", abi=abi, gas=gas.to_dict(),
                              tx=tx, ready=True, reason="READY (test)")

        # --- A. Overpay: simulasi v1 lolos, cek harga v1.1 menolak ---
        print("\n== A. Overpay ==")
        addr_a = deploy()
        fund_agent()
        chain = dict(cfg["chains"]["anvil"], _key="anvil")
        gas = get_fee_data("anvil", tier="seimbang", config_path=cfg_path, w3=w3)
        tx_over = build_mint_tx(w3, chain, addr_a, abi, "mint", [1],
                                5 * PRICE, AGENT, gas)
        sim = simulate(w3, tx_over)
        check("simulasi tx overpay 0.05 ETH LULUS (inilah celah v1)",
              sim["ok"], f"sim={sim}")
        params = {"price": PRICE, "maxPerWallet": 2, "maxSupply": 1}
        reason = check_price_and_quantity(params, 1, 5 * PRICE)
        check("cek harga v1.1 menolak OVERPAY sebelum broadcast",
              reason is not None and "OVERPAY" in reason, f"reason={reason}")

        # --- B. TOCTOU: supply habis sebelum live -> batal tanpa broadcast ---
        print("\n== B. TOCTOU abort ==")
        addr_b = deploy()
        prep_b = make_prep(addr_b, PRICE)
        # Orang lain memborong supply (maxSupply=1) sebelum kita live.
        other = w3.eth.contract(address=addr_b, abi=abi)
        tx_other = other.functions.mint(1).build_transaction(
            {"from": ANVIL_ACCT, "value": PRICE, "gas": 300000})
        send_raw(tx_other)
        bal_before = w3.eth.get_balance(AGENT)
        out_b = execute_live(prep_b, confirm_fn=lambda s: True, config_path=cfg_path)
        check("execute_live BATAL saat sale sudah habis (tanpa broadcast)",
              out_b.ready is False and out_b.tx_hash is None
              and "LIVE DIBATALKAN" in out_b.reason,
              f"ready={out_b.ready} tx_hash={out_b.tx_hash} reason={out_b.reason}")
        check("saldo agent tidak berkurang (tidak ada gas hangus)",
              w3.eth.get_balance(AGENT) == bal_before,
              f"before={bal_before} after={w3.eth.get_balance(AGENT)}")

        # --- C. Happy path: live OK + balance terverifikasi ---
        print("\n== C. Happy path ==")
        addr_c = deploy()
        prep_c = make_prep(addr_c, PRICE)
        out_c = execute_live(prep_c, confirm_fn=lambda s: True, config_path=cfg_path)
        check("execute_live berhasil (receipt status 1)",
              out_c.receipt is not None and out_c.receipt.get("status") == 1,
              f"receipt={out_c.receipt} reason={out_c.reason}")
        check("balanceBefore 0 -> balanceAfter 1, terverifikasi",
              out_c.balance_before == 0 and out_c.balance_after == 1
              and out_c.receipt.get("mint_verified") is True,
              f"before={out_c.balance_before} after={out_c.balance_after} "
              f"verified={out_c.receipt.get('mint_verified') if out_c.receipt else None}")
        print(f"\nHASIL: {PASS} lolos, {len(FAILURES)} gagal: {FAILURES}")
        return 0 if not FAILURES else 1
    finally:
        anvil.terminate()
        try:
            anvil.wait(timeout=10)
        except Exception:
            anvil.kill()


if __name__ == "__main__":
    raise SystemExit(main())
