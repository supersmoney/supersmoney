"""CLI fastmint — jalankan safety + gas + simulasi, cetak PASS/FAIL.

Pemakaian (dari ~/workspace/wl-nft, pakai venv airdrop-agent):
    ~/workspace/airdrop-agent/.venv/bin/python -m fastmint.check_mint \\
        --chain ethereum --contract 0x... --mode dry-run --tier seimbang

    ~/workspace/airdrop-agent/.venv/bin/python -m fastmint.check_mint \\
        --project projects/example.yaml --mode approve --tier hemat

Mode:
  dry-run  — cek + simulasi saja (default, aman).
  approve  — siapkan unsigned tx untuk wallet user (user approve sendiri).
  auto     — siapkan tx untuk wallet agent; broadcast HANYA kalau --live
             ditambah DAN safety PASS DAN gas di bawah cap DAN user
             mengetik 'MINT' saat konfirmasi. Tanpa --live, auto = dry-run.

Exit code:
  0 = READY (website PASS/SKIP, safety PASS, gas di bawah cap, simulasi OK)
  2 = website FAIL / safety FAIL / gas di atas cap / harga tidak cocok /
      build gagal (JANGAN mint)
  3 = simulasi revert (kontrak aman tapi mint tidak bisa jalan sekarang,
      mis. sale belum buka / sold out / tidak eligible)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastmint.common import load_config  # noqa: E402
from fastmint.executor import execute_live, prepare  # noqa: E402


def load_project(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"project yaml tidak valid: {path}")
    return data


def confirm_interactive(summary: str) -> bool:
    print("\n=== KONFIRMASI SEBELUM SIGNING (wajib) ===")
    print(summary)
    print("Ketik MINT (huruf besar) untuk lanjut broadcast, atau Enter untuk batal:")
    try:
        ans = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("Dibatalkan (tidak ada input).")
        return False
    return ans == "MINT"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Fastmint WL NFT — safety + gas + simulasi")
    p.add_argument("--chain", help="nama chain (ethereum, base, bsc, ...)")
    p.add_argument("--contract", help="alamat kontrak NFT")
    p.add_argument("--project", help="path project yaml (boleh menggantikan --chain/--contract)")
    p.add_argument("--mode", default="dry-run", choices=["dry-run", "approve", "auto"],
                   help="mode eksekusi (default dry-run)")
    p.add_argument("--tier", default="seimbang", choices=["hemat", "seimbang", "cepat"],
                   help="tier gas (default seimbang)")
    p.add_argument("--quantity", type=int, default=1, help="jumlah NFT (default 1)")
    p.add_argument("--value-wei", type=int, default=0,
                   help="value yang dikirim, dalam wei (default 0 = free mint)")
    p.add_argument("--live", action="store_true",
                   help="IZINKAN broadcast (hanya mode auto, hanya kalau READY, tetap minta konfirmasi ketik MINT)")
    p.add_argument("--config", default=None, help="path chains.yaml (default config/chains.yaml)")
    p.add_argument("--log", default="logs/mint-checks.jsonl",
                   help="file log JSONL (default logs/mint-checks.jsonl, '' = tanpa log)")
    args = p.parse_args(argv)

    project: dict = {}
    if args.project:
        project = load_project(args.project)
    chain = args.chain or project.get("chain")
    contract = args.contract or project.get("contract")
    if not chain or not contract:
        print("ERROR: chain + contract wajib diisi (via argumen atau project yaml).", file=sys.stderr)
        return 2
    quantity = int(project.get("quantity", args.quantity))
    # Audit v1.1: ekspresi lama `... or 0) * quantity if price else args.value_wei`
    # membuat --value-wei meng-override harga free mint (price=0) — user bisa
    # tanpa sengaja membayar untuk free mint. Sekarang: kalau project yaml
    # menyebut mint_price_wei (termasuk 0), itulah yang dipakai.
    if project.get("mint_price_wei") is not None:
        value_wei = int(project["mint_price_wei"]) * quantity
    else:
        value_wei = args.value_wei
    if quantity < 1:
        print(f"ERROR: quantity harus >= 1 (dapat {quantity}).", file=sys.stderr)
        return 2
    if value_wei < 0:
        print(f"ERROR: value-wei tidak boleh negatif (dapat {value_wei}).", file=sys.stderr)
        return 2
    if project.get("mint_function"):
        pass  # diteruskan ke prepare via project dict

    print(f"fastmint check — chain={chain} contract={contract} mode={args.mode} tier={args.tier} qty={quantity}")

    try:
        prep = prepare(
            chain_name=chain,
            contract=contract,
            mode=args.mode,
            tier=args.tier,
            quantity=quantity,
            value_wei=value_wei,
            project=project,
            config_path=args.config,
        )
    except Exception as e:  # noqa: BLE001 — fail-closed, jangan crash tanpa jejak
        print(f"ERROR: {type(e).__name__}: {e}")
        return 2

    print(f"\nWEBSITE: {prep.website_verdict or 'SKIP'}")
    for c in (prep.website or {}).get("checks", []):
        print(f"  [{c['status']}] {c['name']}: {c['detail']}")
    if (prep.website or {}).get("warnings"):
        print("WEBSITE WARNINGS:")
        for w in prep.website["warnings"]:
            print(f"  - {w}")
    print(f"\nSAFETY: {prep.safety_verdict}")
    for c in prep.safety.get("checks", []):
        print(f"  [{c['status']}] {c['name']}: {c['detail']}")
    if prep.safety.get("blocking_warnings"):
        print("BLOCKING WARNINGS:")
        for w in prep.safety["blocking_warnings"]:
            print(f"  - {w}")
    if prep.gas:
        g = prep.gas
        print(
            f"\nGAS ({g['tier']}): base {g['base_fee_gwei']:.2f} gwei, "
            f"maxPriority {g['max_priority_fee_gwei']:.2f} gwei, "
            f"maxFee {g['max_fee_gwei']:.2f} gwei, limit {g['gas_limit']}, "
            f"estimasi {g['estimated_total_native']:.6f} {g['native_token']} "
            f"(cap {g['cap_native']} {g['native_token']}) -> "
            f"{'DI BAWAH CAP' if g['under_cap'] else 'DI ATAS CAP — JANGAN KIRIM'}"
        )
    print(f"\nSIMULASI: {prep.simulation.get('reason', '-') if prep.simulation else '-'}")
    if prep.tx:
        print("\nTX (unsigned, untuk review):")
        print(json.dumps(prep.tx, indent=2, default=str))
    print(f"\nHASIL: {'READY' if prep.ready else 'NOT READY'} — {prep.reason}")

    # Log (JSONL, tanpa secret)
    if args.log:
        try:
            log_path = Path(args.log)
            if not log_path.is_absolute():
                log_path = Path(__file__).resolve().parent.parent / log_path
            log_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "chain": prep.chain,
                "contract": prep.contract,
                "mode": prep.mode,
                "tier": prep.tier,
                "ready": prep.ready,
                "reason": prep.reason,
                "website_verdict": prep.website_verdict,
                "safety_verdict": prep.safety_verdict,
                "gas": prep.gas,
                "simulation_ok": bool(prep.simulation.get("ok")) if prep.simulation else False,
                "tx_hash": prep.tx_hash,
            }
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            print(f"(log ditulis ke {log_path})")
        except Exception as e:  # noqa: BLE001 — log gagal tidak boleh menggagalkan hasil
            print(f"(log gagal ditulis: {type(e).__name__}: {e})")

    # Live broadcast — hanya mode auto + READY + --live + konfirmasi ketik MINT
    if args.live:
        if prep.mode != "auto":
            print("ERROR: --live hanya berlaku untuk mode auto. Mode approve tidak pernah di-broadcast sistem.")
            return 2
        if not prep.ready:
            print("ERROR: prep NOT READY — live dibatalkan.")
            return 2 if prep.safety_verdict != "PASS" else 3
        print("\nLIVE diminta. Menunggu konfirmasi ketik MINT...")
        try:
            prep = execute_live(prep, confirm_fn=confirm_interactive, config_path=args.config)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR saat live: {type(e).__name__}: {e}")
            return 2
        print(f"\nLIVE: {prep.reason}")
        if prep.receipt:
            print(json.dumps(prep.receipt, indent=2, default=str))
        return 0 if prep.receipt and prep.receipt.get("status") == 1 else 2

    if prep.ready:
        return 0
    if prep.website_verdict == "FAIL":
        return 2
    if prep.safety_verdict != "PASS" or (prep.gas and not prep.gas.get("under_cap")):
        return 2
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
