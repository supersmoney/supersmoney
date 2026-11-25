"""Uji fastmint v1.1 — website gate + perbaikan audit.

Jalankan: ~/workspace/airdrop-agent/.venv/bin/python tests/test_v11.py
Exit 0 = semua lolos. Uji live opensea.io butuh internet; sisanya
mock / pure function (tidak ada broadcast, tidak ada dana).
"""
from __future__ import annotations

import io
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from fastmint import website as W  # noqa: E402
from fastmint.executor import check_price_and_quantity, onchain_unit_price  # noqa: E402
from fastmint.safety import _scan_source, collect_project_sources  # noqa: E402

AZUKI = "0xED5AF388653567Af2F388E6224dC7C4b3241C544"
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


# ---------- Fake HTTP untuk uji halaman (tanpa jaringan) ----------

class FakeRaw:
    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)

    def read(self, n=-1, decode_content=False):  # noqa: ARG002
        return self._buf.read(n)


class FakeHop:
    def __init__(self, url: str):
        self.url = url


class FakeResp:
    def __init__(self, url, status=200, body="", history=None):
        self.url = url
        self.status_code = status
        self.history = history or []
        self.raw = FakeRaw(body.encode())


class FakeSession:
    def __init__(self, resp=None, exc=None):
        self.resp = resp
        self.exc = exc
        self.max_redirects = 5

    def get(self, *a, **k):  # noqa: ARG002
        if self.exc is not None:
            raise self.exc
        return self.resp


_orig_session = requests.Session


def with_fake_session(resp=None, exc=None):
    W.requests.Session = lambda: FakeSession(resp, exc)  # noqa: E731


def restore_session():
    W.requests.Session = _orig_session


# ---------- 1. Website gate ----------

def test_website() -> None:
    print("\n== Website gate ==")
    # (1) Live: domain resmi opensea.io -> PASS (hanya cek, tidak mint).
    r = W.check_website(
        {"official_website": "opensea.io", "contract": AZUKI},
        contract=AZUKI,
    )
    check("live opensea.io homepage -> PASS", r.verdict == "PASS",
          f"verdict={r.verdict} checks={[(c.name, c.status) for c in r.checks]}")

    # (2) Typosquat -> FAIL (tanpa jaringan, host sudah beda).
    r = W.check_website(
        {"official_website": "opensea.io", "mint_url": "https://opensea-io.com/mint",
         "contract": AZUKI},
        contract=AZUKI,
    )
    check("typosquat opensea-io.com -> FAIL", r.verdict == "FAIL", f"verdict={r.verdict}")

    # (3) Punycode / homograph -> FAIL.
    r = W.check_website(
        {"official_website": "opensea.io", "mint_url": "https://xn--opensea-abc.com/mint",
         "contract": AZUKI},
        contract=AZUKI,
    )
    check("punycode xn-- -> FAIL", r.verdict == "FAIL", f"verdict={r.verdict}")
    reason = W._looks_like_typosquat("xn--opensea-abc.com", "opensea.io")
    check("helper typosquat deteksi xn--", reason is not None, f"reason={reason}")
    reason = W._looks_like_typosquat("opensea.io", "opensea.io")
    check("helper typosquat: domain sama -> None", reason is None, f"reason={reason}")

    # (4) HTTP (tanpa s) -> FAIL.
    r = W.check_website(
        {"official_website": "opensea.io", "mint_url": "http://opensea.io/mint",
         "contract": AZUKI},
        contract=AZUKI,
    )
    check("http:// -> FAIL", r.verdict == "FAIL", f"verdict={r.verdict}")

    # (5) Project yaml tanpa official_website -> FAIL.
    r = W.check_website({"mint_url": "https://opensea.io/mint", "contract": AZUKI},
                        contract=AZUKI)
    check("tanpa official_website -> FAIL", r.verdict == "FAIL", f"verdict={r.verdict}")

    # (6) Tanpa project yaml -> SKIP (mint langsung ke kontrak).
    r = W.check_website(None, contract=AZUKI)
    check("tanpa project yaml -> SKIP", r.verdict == "SKIP", f"verdict={r.verdict}")

    # (7) Mock: halaman minta seed phrase -> FAIL.
    body = ("<html><body><h1>Mint now</h1>"
            "<p>Enter your seed phrase to continue</p></body></html>")
    with_fake_session(FakeResp("https://example.com/mint", 200, body))
    try:
        r = W.check_website(
            {"official_website": "example.com", "mint_url": "https://example.com/mint",
             "contract": "0x1111111111111111111111111111111111111111"},
            contract="0x1111111111111111111111111111111111111111",
        )
    finally:
        restore_session()
    check("halaman minta seed phrase -> FAIL", r.verdict == "FAIL", f"verdict={r.verdict}")

    # (8) Mock: alamat kontrak di halaman beda -> FAIL (mint_url eksplisit).
    body = ("<html><body>Contract: 0x2222222222222222222222222222222222222222"
            "</body></html>")
    with_fake_session(FakeResp("https://example.com/mint", 200, body))
    try:
        r = W.check_website(
            {"official_website": "example.com", "mint_url": "https://example.com/mint",
             "contract": "0x1111111111111111111111111111111111111111"},
            contract="0x1111111111111111111111111111111111111111",
        )
    finally:
        restore_session()
    check("kontrak di halaman beda -> FAIL", r.verdict == "FAIL", f"verdict={r.verdict}")

    # (9) Mock: alamat kontrak cocok -> PASS.
    body = ("<html><body>Contract: 0x1111111111111111111111111111111111111111"
            "<a href=\"/mint\">Connect Wallet</a></body></html>")
    with_fake_session(FakeResp("https://example.com/mint", 200, body))
    try:
        r = W.check_website(
            {"official_website": "example.com", "mint_url": "https://example.com/mint",
             "contract": "0x1111111111111111111111111111111111111111"},
            contract="0x1111111111111111111111111111111111111111",
        )
    finally:
        restore_session()
    check("kontrak di halaman cocok -> PASS", r.verdict == "PASS",
          f"verdict={r.verdict} checks={[(c.name, c.status) for c in r.checks]}")

    # (10) Mock: tombol connect wallet ke domain lain -> FAIL.
    body = ("<html><body><a href=\"https://evil-connect.com/\">Connect Wallet</a>"
            "</body></html>")
    with_fake_session(FakeResp("https://example.com/mint", 200, body))
    try:
        r = W.check_website(
            {"official_website": "example.com", "mint_url": "https://example.com/mint",
             "contract": "0x1111111111111111111111111111111111111111"},
            contract="0x1111111111111111111111111111111111111111",
        )
    finally:
        restore_session()
    check("connect wallet ke domain lain -> FAIL", r.verdict == "FAIL", f"verdict={r.verdict}")

    # (11) Mock: redirect lewat domain lain -> FAIL.
    hop = FakeHop("https://evil-redirect.com/step")
    with_fake_session(FakeResp("https://example.com/mint", 200, "<html></html>",
                               history=[hop]))
    try:
        r = W.check_website(
            {"official_website": "example.com", "mint_url": "https://example.com/mint",
             "contract": "0x1111111111111111111111111111111111111111"},
            contract="0x1111111111111111111111111111111111111111",
        )
    finally:
        restore_session()
    check("redirect lewat domain lain -> FAIL", r.verdict == "FAIL", f"verdict={r.verdict}")


# ---------- 2. Audit: harga + quantity ----------

def test_price_quantity() -> None:
    print("\n== Audit harga + quantity ==")
    azuki_params = {
        "saleConfig": [0, 1674145770, 500000000000000000, 0, 0],
        "maxPerAddressDuringMint": 5,
    }
    price, src = onchain_unit_price(azuki_params)
    check("harga Azuki terbaca dari saleConfig[2] = 0.5 ETH",
          price == 500000000000000000 and src == "saleConfig[2]",
          f"price={price} src={src}")

    # v1: tx value 0 untuk Azuki lolos sampai simulasi (revert) — tapi
    # overpay TIDAK ketangkap simulasi. Bukti celah: kontrak yang
    # menerima msg.value >= price akan READY dengan value berlebih.
    # v1.1: cek harga menolak keduanya sebelum build.
    r = check_price_and_quantity(azuki_params, 1, 0)
    check("Azuki qty1 value 0 -> ditolak (underpay)",
          r is not None and "TIDAK COCOK" in r, f"reason={r}")
    r = check_price_and_quantity(azuki_params, 1, 500000000000000000)
    check("Azuki qty1 value 0.5 ETH -> OK", r is None, f"reason={r}")
    r = check_price_and_quantity(azuki_params, 1, 1000000000000000000)
    check("Azuki qty1 value 1 ETH -> ditolak (OVERPAY)",
          r is not None and "OVERPAY" in r, f"reason={r}")
    r = check_price_and_quantity(azuki_params, 6, 3000000000000000000)
    check("Azuki qty6 > max 5 -> ditolak (quantity)",
          r is not None and "MELEBIHI BATAS" in r, f"reason={r}")
    r = check_price_and_quantity(azuki_params, 0, 0)
    check("quantity 0 -> ditolak", r is not None and "QUANTITY" in r, f"reason={r}")
    r = check_price_and_quantity({"price": 100, "maxPerWallet": 3}, 2, 250)
    check("price key eksplisit: 2x100 != 250 -> ditolak",
          r is not None and "TIDAK COCOK" in r, f"reason={r}")
    r = check_price_and_quantity({"price": 100}, 2, 200)
    check("price key eksplisit: 2x100 == 200 -> OK", r is None, f"reason={r}")
    r = check_price_and_quantity({}, 1, 0)
    check("harga tidak terbaca -> tidak diblokir (WARN saja)",
          r is None, f"reason={r}")

    # Bug value_wei di check_mint v1: --value-wei meng-override free mint.
    mint_price_wei, cli_value = 0, 10 ** 18
    old = int(mint_price_wei or 0) * 1 if mint_price_wei else cli_value
    new = int(mint_price_wei) * 1 if mint_price_wei is not None else cli_value
    check("bug value_wei v1: free mint + --value-wei 1 ETH -> lama 1 ETH, baru 0",
          old == 10 ** 18 and new == 0, f"old={old} new={new}")


# ---------- 3. Audit: scan source semua file ----------

def test_source_scan() -> None:
    print("\n== Audit scan source ==")
    main_clean = "contract Mint is ERC721 { function mint(uint256 q) external payable {} }"
    imported_drain = (
        "contract Helper { function drain() external { "
        "payable(msg.sender).transfer(address(this).balance); } }"
    )
    sources = {
        "contracts/Mint.sol": {"content": main_clean},
        "contracts/Helper.sol": {"content": imported_drain},
        "@openzeppelin/contracts/token/ERC721/ERC721.sol": {"content": "contract ERC721 {}"},
    }
    name, main, all_text = collect_project_sources(sources)
    check("collect: nama + file utama ditemukan",
          name == "Mint" and main == main_clean, f"name={name}")
    check("collect: file import ikut terkumpul",
          all_text is not None and "drain" in all_text, f"all={bool(all_text)}")
    # v1 hanya scan file utama -> drain di Helper lolos.
    old_findings = _scan_source(main_clean, ["mint"])
    new_findings = _scan_source(all_text or "", ["mint"])
    check("v1 (file utama saja): drain di file import TIDAK ketemu",
          old_findings == [], f"old={old_findings}")
    check("v1.1 (semua file): drain di file import KETEMU",
          len(new_findings) > 0, f"new={new_findings}")


# ---------- 4. Audit: .gitignore menahan wl_proof ----------

def test_gitignore() -> None:
    print("\n== Audit .gitignore ==")
    base = Path(__file__).resolve().parent.parent
    text = (base / ".gitignore").read_text()
    check(".gitignore menahan projects/*.yaml",
          "projects/*.yaml" in text, "tidak ada baris projects/*.yaml")
    check(".gitignore mengecualikan example.yaml",
          "!projects/example.yaml" in text, "tidak ada pengecualian example.yaml")
    # Bukti nyata via git check-ignore di repo temp.
    try:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / ".gitignore").write_text(text)
            (td / "projects").mkdir()
            (td / "projects" / "rahasia.yaml").write_text("wl_proof: [\"0xabc\"]\n")
            (td / "projects" / "example.yaml").write_text("project: CONTOH\n")
            subprocess.run(["git", "init", "-q"], cwd=td, check=True)
            # check-ignore exit 0 = di-ignore, 1 = tidak di-ignore.
            # (Jangan pakai -v dengan dua file: pola negasi !example.yaml
            # ikut tampil di stdout dan mengacaukan assert string.)
            secret = subprocess.run(
                ["git", "check-ignore", "-q", "projects/rahasia.yaml"],
                cwd=td, capture_output=True)
            example = subprocess.run(
                ["git", "check-ignore", "-q", "projects/example.yaml"],
                cwd=td, capture_output=True)
            check("git check-ignore: rahasia.yaml di-ignore, example.yaml tidak",
                  secret.returncode == 0 and example.returncode == 1,
                  f"secret_rc={secret.returncode} example_rc={example.returncode}")
    except FileNotFoundError:
        print("  (git tidak tersedia — cek isi file saja)")


def main() -> int:
    test_website()
    test_price_quantity()
    test_source_scan()
    test_gitignore()
    print(f"\nHASIL: {PASS} lolos, {len(FAILURES)} gagal: {FAILURES}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
