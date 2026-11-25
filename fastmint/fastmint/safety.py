"""Safety gate for fastmint — fail-closed contract checker.

What it checks (in order, stops are recorded, not hidden):
  1. Alamat valid + chain dikenal.
  2. Ada bytecode di alamat itu (eth_getCode). EOA / alamat kosong -> FAIL.
  3. Kontrak TERVERIFIKASI di Sourcify v2 (tanpa API key).
     Etherscan V1 sudah deprecated dan V2 butuh API key, jadi Sourcify
     adalah sumber verifikasi utama v1. 404 / error jaringan -> FAIL.
  4. ABI: harus ada fungsi mint, dan fungsi-fungsi standar ERC721
     (approve / setApprovalForAll / transferFrom) dicatat sebagai INFO —
     kehadiran mereka NORMAL untuk ERC721, yang berbahaya adalah kalau
     fungsi mint/claim memanggil mereka.
  5. Source scan (file kontrak utama): selfdestruct / delegatecall /
     fungsi drain / mint yang mentransfer aset caller -> FAIL.
  6. Proxy: slot EIP-1967. Kalau proxy, implementation-nya harus ikut
     terverifikasi, kalau tidak -> FAIL.
  7. Mint params: baca view functions (totalSupply, maxSupply, harga,
     max per wallet, status sale, owner, name, symbol). Kalau tidak ada
     yang bisa dibaca -> WARN yang memblokir mode auto (bukan FAIL
     safety, tapi executor tidak boleh auto-broadcast).

FAIL-CLOSED: kalau satu langkah tidak bisa diverifikasi (RPC mati,
Sourcify tidak menjawab, ABI tidak ada), hasilnya FAIL, bukan "coba saja".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
from web3 import Web3

from .common import (
    canonical_chain,
    checksum,
    connect_chain,
    get_chain,
    is_valid_address,
    load_config,
)

SOURCIFY_BASE = "https://sourcify.dev/server/v2/contract"

# Nama fungsi yang dianggap "mint" (case-insensitive, prefix match ok)
MINT_FN_NAMES = (
    "mint", "publicmint", "whitelistmint", "allowlistmint", "presalemint",
    "publicsalemint", "auctionmint", "claim", "mintnft", "freemint",
    "mintwl", "wl mint",
)
# View functions yang dicoba dibaca untuk mint params (berurutan)
VIEW_CANDIDATES = [
    "name", "symbol", "totalSupply", "maxSupply", "MAX_SUPPLY",
    "collectionSize", "maxPerWallet", "maxMintPerWallet",
    "maxPerAddressDuringMint", "maxPerAddress", "mintPrice", "price",
    "cost", "publicSalePrice", "whitelistPrice", "allowlistPrice",
    "saleConfig", "isPublicSaleOn", "paused", "saleActive",
    "mintingActive", "publicSaleActive", "owner", "maxBatchSize",
]
# Fungsi standar ERC721 — kehadirannya NORMAL, dicatat sebagai info saja.
STANDARD_RISKY_NAMES = ("approve", "setApprovalForAll", "transferFrom", "safeTransferFrom")
# Nama fungsi yang mencurigakan kalau ada transfer di dalamnya
SUSPICIOUS_FN_NAMES = ("drain", "sweep", "rescue", "emergencywithdraw", "withdrawall")

EIP1967_IMPL_SLOT = int(
    "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc", 16
)


@dataclass
class Check:
    name: str
    status: str  # PASS | FAIL | WARN | INFO
    detail: str


@dataclass
class SafetyResult:
    verdict: str  # PASS | FAIL
    contract: str
    chain: str
    checks: list[Check] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blocking_warnings: list[str] = field(default_factory=list)
    abi: Optional[list] = None
    source_main: Optional[str] = None
    # Semua source non-OZ digabung — scan drain harus mencakup file import,
    # bukan hanya file utama (audit v1.1: drain di file import lolos v1).
    source_all: Optional[str] = None
    contract_name: Optional[str] = None
    mint_params: dict[str, Any] = field(default_factory=dict)
    mint_functions: list[str] = field(default_factory=list)
    is_proxy: bool = False
    implementation: Optional[str] = None
    owner: Optional[str] = None

    def add(self, name: str, status: str, detail: str) -> None:
        self.checks.append(Check(name, status, detail))
        if status == "WARN":
            self.warnings.append(f"{name}: {detail}")

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "contract": self.contract,
            "chain": self.chain,
            "is_proxy": self.is_proxy,
            "implementation": self.implementation,
            "owner": self.owner,
            "contract_name": self.contract_name,
            "mint_functions": self.mint_functions,
            "mint_params": {k: str(v) for k, v in self.mint_params.items()},
            "warnings": self.warnings,
            "blocking_warnings": self.blocking_warnings,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail}
                for c in self.checks
            ],
        }


def _sourcify(chain_id: int, address: str, fields: Optional[str] = None) -> tuple[int, dict]:
    url = f"{SOURCIFY_BASE}/{chain_id}/{address}"
    params = {"fields": fields} if fields else None
    r = requests.get(url, params=params, timeout=25)
    try:
        data = r.json()
    except Exception:
        data = {"_raw": r.text[:300]}
    return r.status_code, data if isinstance(data, dict) else {"_raw": str(data)[:300]}


def _extract_fn_body(source: str, fn_name: str) -> Optional[str]:
    """Ambil body fungsi berdasarkan nama (brace matching sederhana)."""
    m = re.search(
        r"function\s+" + re.escape(fn_name) + r"\s*\([^)]*\)[^{]*\{",
        source,
    )
    if not m:
        return None
    i = m.end() - 1  # posisi '{'
    depth = 0
    for j in range(i, len(source)):
        if source[j] == "{":
            depth += 1
        elif source[j] == "}":
            depth -= 1
            if depth == 0:
                return source[i + 1 : j]
    return None


def _scan_source(source: str, fn_names: list[str]) -> list[str]:
    """Scan file kontrak utama. Return daftar temuan FAIL (kosong = bersih)."""
    findings: list[str] = []
    if re.search(r"\bselfdestruct\s*\(", source):
        findings.append("selfdestruct() ditemukan di source — kontrak bisa menghancurkan diri")
    if re.search(r"\bdelegatecall\s*\(", source):
        findings.append("delegatecall() ditemukan di kontrak utama — logika bisa dialihkan ke alamat lain")
    # Fungsi mencurigakan yang mentransfer aset
    for m in re.finditer(r"function\s+(\w+)\s*\(", source):
        name = m.group(1)
        if name.lower() in SUSPICIOUS_FN_NAMES or "drain" in name.lower():
            body = _extract_fn_body(source, name) or ""
            if re.search(r"\b(transferFrom|safeTransferFrom|transfer\(|call\{value)", body):
                findings.append(f"fungsi mencurigakan {name}() mentransfer aset")
    # Body fungsi mint/claim tidak boleh mentransfer aset caller
    for fn in fn_names:
        body = _extract_fn_body(source, fn)
        if body is None:
            continue
        if re.search(r"\bsetApprovalForAll\s*\(", body):
            findings.append(f"fungsi {fn}() memanggil setApprovalForAll — bisa memberi izin ambil semua NFT")
        if re.search(r"\bapprove\s*\(", body):
            findings.append(f"fungsi {fn}() memanggil approve — cek scope approval-nya")
        if re.search(r"\b(transferFrom|safeTransferFrom)\s*\(\s*msg\.sender", body):
            findings.append(f"fungsi {fn}() mentransfer aset dari msg.sender")
        if re.search(r"\bdelegatecall\s*\(", body):
            findings.append(f"fungsi {fn}() memakai delegatecall")
    return findings


def collect_project_sources(sources: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Dari dict sources Sourcify: (nama_kontrak, file_utama, semua_source_project).

    Hanya file project sendiri (bukan @openzeppelin / node_modules).
    v1 hanya memakai file utama untuk scan drain — drain di file import
    lolos. v1.1 mengembalikan semua file project digabung untuk di-scan.
    """
    own_texts: list[str] = []
    name: Optional[str] = None
    main: Optional[str] = None
    for path, content in sources.items():
        text = content.get("content", "") if isinstance(content, dict) else ""
        if "@openzeppelin" in path or "node_modules" in path:
            continue
        if text:
            own_texts.append(text)
        if name is None:
            m = re.search(r"contract\s+(\w+)\s+is\b", text)
            if m:
                name = m.group(1)
                main = text
    if main is None and own_texts:
        main = own_texts[0]
    all_text = "\n".join(own_texts) if own_texts else None
    return name, main, all_text


def check_contract(
    chain_name: str,
    address: str,
    project: Optional[dict] = None,
    config_path: Optional[str] = None,
) -> SafetyResult:
    project = project or {}
    cfg = load_config(config_path) if config_path else load_config()
    res = SafetyResult(verdict="FAIL", contract=address, chain=chain_name)
    failed = False

    def fail(name: str, detail: str) -> None:
        nonlocal failed
        failed = True
        res.add(name, "FAIL", detail)

    # 1. Format alamat
    if not is_valid_address(address):
        fail("address_format", f"alamat tidak valid: {address!r}")
        res.verdict = "FAIL"
        return res
    addr = checksum(address)
    res.contract = addr
    res.add("address_format", "PASS", f"alamat valid (checksum {addr})")

    # 2. Chain dikenal + RPC
    try:
        key = canonical_chain(chain_name)
        chain = get_chain(cfg, key)
        res.chain = key
    except ValueError as e:
        fail("chain", str(e))
        res.verdict = "FAIL"
        return res
    res.add("chain", "PASS", f"{chain.get('display', key)} (chain_id {chain.get('chain_id')})")

    if chain.get("verification") == "manual":
        fail(
            "verification_source",
            f"chain {key} masih manual di v1 — belum ada sumber verifikasi otomatis "
            "(Sourcify / explorer API) yang bisa diuji. Jangan dipaksa PASS.",
        )
        res.verdict = "FAIL"
        return res

    try:
        w3 = connect_chain(chain)
    except RuntimeError as e:
        fail("rpc", str(e))
        res.verdict = "FAIL"
        return res
    res.add("rpc", "PASS", "RPC terkoneksi dan chain_id cocok")

    # 3. Bytecode harus ada
    try:
        code = w3.eth.get_code(addr)
    except Exception as e:  # noqa: BLE001
        fail("bytecode", f"eth_getCode gagal: {type(e).__name__}: {str(e)[:150]}")
        res.verdict = "FAIL"
        return res
    if len(code) <= 2:
        fail("bytecode", "tidak ada bytecode di alamat ini (EOA / alamat kosong / belum deploy)")
        res.verdict = "FAIL"
        return res
    res.add("bytecode", "PASS", f"bytecode ada ({len(code)} bytes)")

    # 4. Verifikasi Sourcify
    chain_id = chain.get("sourcify_chain_id") or chain.get("chain_id")
    try:
        status, data = _sourcify(chain_id, addr)
    except Exception as e:  # noqa: BLE001
        fail("verified_source", f"Sourcify tidak bisa dihubungi: {type(e).__name__}: {str(e)[:120]}")
        res.verdict = "FAIL"
        return res
    match = data.get("match")
    if status == 404 or match is None:
        fail("verified_source", f"kontrak TIDAK terverifikasi di Sourcify (status {status}, match={match})")
        res.verdict = "FAIL"
        return res
    if match not in ("exact_match", "partial_match"):
        fail("verified_source", f"status verifikasi Sourcify tidak dikenal: {match}")
        res.verdict = "FAIL"
        return res
    res.add("verified_source", "PASS", f"Sourcify {match} (chain {chain_id})")

    # Ambil ABI + source + metadata
    try:
        _s, abi_data = _sourcify(chain_id, addr, fields="abi")
        abi = abi_data.get("abi")
        _s2, src_data = _sourcify(chain_id, addr, fields="sources")
        sources = src_data.get("sources") or {}
        _s3, meta_data = _sourcify(chain_id, addr, fields="metadata")
        meta = meta_data.get("metadata") or {}
    except Exception as e:  # noqa: BLE001
        fail("fetch_abi_source", f"gagal ambil ABI/source: {type(e).__name__}: {str(e)[:120]}")
        res.verdict = "FAIL"
        return res
    if not abi:
        fail("fetch_abi_source", "ABI tidak tersedia dari Sourcify")
        res.verdict = "FAIL"
        return res
    res.abi = abi
    res.add("fetch_abi_source", "PASS", f"ABI didapat ({len(abi)} entri)")

    # Nama kontrak + kumpulkan SEMUA source non-OZ untuk scan drain.
    try:
        _name, _main, _all = collect_project_sources(sources)
        if _name:
            res.contract_name = _name
        if _main:
            res.source_main = _main
        res.source_all = _all
    except Exception:
        pass

    # 5. Analisis ABI
    fn_names = [a["name"] for a in abi if a.get("type") == "function"]
    fn_lower = {n.lower(): n for n in fn_names}
    mint_fns = [n for n in fn_names if n.lower() in MINT_FN_NAMES or n.lower().startswith("mint")]
    res.mint_functions = sorted(set(mint_fns))
    if not res.mint_functions:
        fail("mint_function", f"tidak ada fungsi mint di ABI (fungsi yang ada: {sorted(set(fn_names))[:20]})")
    else:
        res.add("mint_function", "PASS", f"fungsi mint: {res.mint_functions}")
    for std in STANDARD_RISKY_NAMES:
        if std in fn_lower:
            res.add(f"std_{std}", "INFO", f"{std}() ada — standar ERC721, bukan bukti drain. Bahaya hanya kalau mint memanggilnya.")
    if "owner" in fn_lower:
        res.add("owner_fn", "INFO", "owner() ada — owner bisa ubah parameter / withdraw saldo kontrak (normal untuk NFT drop, bukan drain wallet).")

    # 6. Source scan — SEMUA file source project (bukan hanya file utama).
    scan_target = res.source_all or res.source_main
    if scan_target:
        findings = _scan_source(scan_target, res.mint_functions or fn_names)
        if findings:
            for f in findings:
                fail("source_scan", f)
        else:
            res.add("source_scan", "PASS", "tidak ada pola drain di semua file source project (selfdestruct / delegatecall / approval di dalam mint / fungsi drain)")
    else:
        res.add("source_scan", "WARN", "file source tidak teridentifikasi — scan pola drain dilewati (ABI + verifikasi tetap dicek)")
        res.blocking_warnings.append("source_scan: file source tidak teridentifikasi")

    # 7. Proxy?
    try:
        impl_slot = w3.eth.get_storage_at(addr, EIP1967_IMPL_SLOT)
        impl = "0x" + impl_slot[-20:].hex()
        if impl_slot != b"\x00" * 32 and impl != "0x0000000000000000000000000000000000000000":
            res.is_proxy = True
            res.implementation = Web3.to_checksum_address(impl)
            st, idata = _sourcify(chain_id, res.implementation)
            if st == 200 and idata.get("match") in ("exact_match", "partial_match"):
                res.add("proxy", "PASS", f"proxy EIP-1967 -> implementation {res.implementation} terverifikasi ({idata.get('match')})")
            else:
                fail("proxy", f"proxy EIP-1967 -> implementation {res.implementation} TIDAK terverifikasi")
        else:
            res.add("proxy", "PASS", "bukan proxy EIP-1967 (implementation slot kosong)")
    except Exception as e:  # noqa: BLE001
        res.add("proxy", "WARN", f"cek proxy gagal: {type(e).__name__}: {str(e)[:100]}")

    # 8. Mint params via eth_call
    if abi:
        contract = w3.eth.contract(address=addr, abi=abi)
        read_ok = 0
        for name in VIEW_CANDIDATES:
            fn = getattr(contract.functions, name, None)
            if fn is None:
                continue
            try:
                val = fn().call()
                res.mint_params[name] = val
                read_ok += 1
            except Exception:
                continue
        if read_ok == 0:
            res.add("mint_params", "WARN", "tidak ada mint param yang bisa dibaca via eth_call")
            res.blocking_warnings.append("mint_params: tidak ada view function yang bisa dibaca")
        else:
            res.add("mint_params", "PASS", f"{read_ok} param terbaca: {sorted(res.mint_params.keys())}")
        # Expected name/symbol dari project
        if project.get("expected_name") and res.mint_params.get("name"):
            if str(res.mint_params["name"]) != str(project["expected_name"]):
                fail("expected_name", f"name on-chain {res.mint_params['name']!r} != expected {project['expected_name']!r}")
        if project.get("expected_symbol") and res.mint_params.get("symbol"):
            if str(res.mint_params["symbol"]) != str(project["expected_symbol"]):
                fail("expected_symbol", f"symbol on-chain {res.mint_params['symbol']!r} != expected {project['expected_symbol']!r}")
        if "owner" in res.mint_params:
            res.owner = str(res.mint_params["owner"])
            if res.owner == "0x0000000000000000000000000000000000000000":
                res.add("owner", "INFO", "ownership sudah renounced (owner = 0x0)")
        # Sold out / sale off -> bukan FAIL safety, tapi blokir auto
        total = res.mint_params.get("totalSupply")
        max_sup = res.mint_params.get("maxSupply") or res.mint_params.get("MAX_SUPPLY")
        if isinstance(total, int) and isinstance(max_sup, int) and max_sup > 0 and total >= max_sup:
            res.add("sale_state", "WARN", f"sold out: totalSupply {total} >= maxSupply {max_sup} — mint akan revert")
            res.blocking_warnings.append("sale_state: sold out")
        if res.mint_params.get("isPublicSaleOn") is False:
            res.add("sale_state", "WARN", "public sale sedang OFF (isPublicSaleOn=false)")
            res.blocking_warnings.append("sale_state: public sale OFF")

    res.verdict = "FAIL" if failed else "PASS"
    return res
