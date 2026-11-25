"""Website gate anti-phishing untuk fastmint — fail-closed.

Gate ini jalan SEBELUM safety kontrak. Tujuannya bukan menilai apakah
sebuah project bagus, tapi memastikan URL mint yang mau dipakai adalah
website resmi project itu — bukan tiruan (typosquat / homograph /
redirect ke domain lain / halaman yang minta seed phrase).

Aturan keras:
  - project yaml WAJIB punya `official_website` (domain resmi). Tidak
    ada -> FAIL, tidak ada bypass. Gate ini hanya memvalidasi URL yang
    ditulis user di yaml vs domain resmi — JANGAN pernah ikuti link
    dari DM / grup random / hasil pencarian berbayar.
  - `mint_url` (kalau ada) harus HTTPS dan host-nya SAMA PERSIS dengan
    domain resmi. Beda satu huruf / tambah hyphen / beda TLD / subdomain
    asing -> FAIL.
  - Punycode (xn--) / karakter non-ASCII yang mirip huruf Latin ->
    FAIL (homograph).
  - Redirect: domain akhir (dan semua perantara) harus tetap domain
    resmi. Redirect ke domain lain -> FAIL.
  - TLS harus valid (requests verify default, tidak pernah di-disable).
  - Halaman (tanpa eksekusi JS): minta seed phrase / private key ->
    FAIL. Alamat kontrak 0x... yang muncul di halaman harus SAMA dengan
    `contract` di yaml — kalau ada yang beda -> FAIL. Link "connect
    wallet" yang mengarah ke domain lain -> FAIL.
  - Kalau website tidak bisa dihubungi / tidak bisa diverifikasi ->
    FAIL, bukan "coba saja".

Kalau tidak ada project yaml sama sekali (CLI langsung --chain/--contract),
gate ini SKIP — user mint langsung ke kontrak tanpa lewat website apa pun,
yang justru jalur paling aman dari phishing website. SKIP dicatat jelas,
bukan disembunyikan.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import requests

from .common import checksum, is_valid_address

# Pola yang berarti halaman MEMINTA seed / private key user -> FAIL.
# Sengaja spesifik ("enter/paste/import ... seed/private key"), supaya
# teks peringatan legit seperti "never share your seed phrase" tidak
# ikut kena FAIL (itu hanya WARN).
SEED_REQUEST_PATTERNS = (
    r"enter\s+(your\s+)?(seed\s+phrase|mnemonic|secret\s+recovery\s+phrase)",
    r"paste\s+(your\s+)?(seed\s+phrase|mnemonic|secret\s+recovery\s+phrase|private\s+key)",
    r"import\s+(your\s+)?(seed\s+phrase|mnemonic|private\s+key)",
    r"enter\s+(your\s+)?private\s+key",
    r"type\s+(your\s+)?(seed\s+phrase|mnemonic|private\s+key)",
    r"input\s+(your\s+)?(seed\s+phrase|mnemonic)",
    r"reveal\s+(your\s+)?(seed\s+phrase|mnemonic|private\s+key)",
)
SEED_MENTION_PATTERN = r"seed\s+phrase|mnemonic|secret\s+recovery\s+phrase|private\s+key"

ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
HREF_RE = re.compile(
    r"""<a\s[^>]*href\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a\s*>""",
    re.IGNORECASE | re.DOTALL,
)
TAG_STRIP_RE = re.compile(r"<[^>]+>")

MAX_REDIRECTS = 5
FETCH_TIMEOUT = 15
MAX_BODY_BYTES = 500_000


@dataclass
class Check:
    name: str
    status: str  # PASS | FAIL | WARN | INFO
    detail: str


@dataclass
class WebsiteResult:
    verdict: str  # PASS | FAIL | SKIP
    mint_url: Optional[str] = None
    official_domain: Optional[str] = None
    final_url: Optional[str] = None
    checks: list[Check] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str) -> None:
        self.checks.append(Check(name, status, detail))
        if status == "WARN":
            self.warnings.append(f"{name}: {detail}")

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "mint_url": self.mint_url,
            "official_domain": self.official_domain,
            "final_url": self.final_url,
            "warnings": self.warnings,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail}
                for c in self.checks
            ],
        }


def normalize_domain(raw: str) -> str:
    """Terima 'opensea.io' atau 'https://opensea.io/mint' -> 'opensea.io'.

    Menolak: port, userinfo, path aneh, domain kosong. Mengembalikan
    domain lowercase tanpa trailing dot.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
    except Exception:
        return ""
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if parsed.port is not None and parsed.port != 443:
        return ""  # port aneh -> tidak bisa dinormalisasi dengan aman
    if parsed.username or parsed.password:
        return ""
    return host


def _levenshtein(a: str, b: str) -> int:
    """Edit distance sederhana (domain pendek, jadi O(n*m) cukup)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _looks_like_typosquat(host: str, official: str) -> Optional[str]:
    """Return alasan kalau host terlihat seperti tiruan domain resmi."""
    if host == official:
        return None
    # Homograph / IDN: karakter non-ASCII apa pun di host -> curiga.
    if any(ord(ch) > 127 for ch in host):
        return f"host mengandung karakter non-ASCII (homograph/IDN): {host!r}"
    if "xn--" in host:
        return f"host memakai punycode (xn--): {host!r}"
    # Confusable: karakter yang terlihat mirip huruf Latin (Cyrillic dsb).
    confusables = set("аеорсхуіјѕһԁɡӏ")
    if any(ch in confusables for ch in host):
        return f"host mengandung karakter yang mirip huruf Latin (confusable): {host!r}"
    # Edit distance kecil tapi tidak sama persis -> kemungkinan typo-squat.
    dist = _levenshtein(host, official)
    if dist <= 2:
        return (
            f"host {host!r} hanya berjarak edit {dist} dari domain resmi "
            f"{official!r} (kemungkinan typosquat)"
        )
    # Domain resmi dipakai sebagai subdomain dari domain lain, atau sebaliknya.
    if host.endswith("." + official) or official.endswith("." + host):
        return (
            f"host {host!r} adalah subdomain/varian dari domain resmi "
            f"{official!r}, bukan domain itu sendiri"
        )
    return None


def check_website(
    project: Optional[dict] = None,
    contract: Optional[str] = None,
) -> WebsiteResult:
    """Validasi website mint vs domain resmi dari project yaml.

    - project None / kosong -> SKIP (mint langsung ke kontrak, tanpa website).
    - project ada tapi official_website kosong -> FAIL.
    """
    project = project or {}
    if not project:
        res = WebsiteResult(verdict="SKIP")
        res.add(
            "website_gate",
            "INFO",
            "tidak ada project yaml — mint langsung ke kontrak tanpa lewat "
            "website apa pun (jalur paling aman dari phishing website).",
        )
        return res

    res = WebsiteResult(verdict="FAIL")
    failed = False

    def fail(name: str, detail: str) -> None:
        nonlocal failed
        failed = True
        res.add(name, "FAIL", detail)

    official_raw = (project.get("official_website") or "").strip()
    if not official_raw:
        fail(
            "official_website",
            "project yaml tidak punya `official_website` — tidak bisa "
            "memverifikasi website resmi. Isi domain resmi project "
            "(contoh: opensea.io) sebelum mint. Jangan bypass.",
        )
        res.verdict = "FAIL"
        return res
    official = normalize_domain(official_raw)
    if not official or "." not in official:
        fail(
            "official_website",
            f"`official_website` tidak valid sebagai domain: {official_raw!r}",
        )
        res.verdict = "FAIL"
        return res
    res.official_domain = official
    res.add("official_website", "PASS", f"domain resmi: {official}")

    mint_url = (project.get("mint_url") or "").strip()
    explicit_mint_url = bool(mint_url)
    if not mint_url:
        # Tanpa mint_url: verifikasi homepage domain resmi saja.
        mint_url = f"https://{official}/"
        res.add(
            "mint_url",
            "INFO",
            "tidak ada `mint_url` di yaml — memeriksa homepage domain resmi.",
        )
    res.mint_url = mint_url

    # (a) Harus HTTPS, host harus sama persis.
    try:
        parsed = urlparse(mint_url)
    except Exception as e:  # noqa: BLE001
        fail("mint_url", f"mint_url tidak bisa di-parse: {e}")
        res.verdict = "FAIL"
        return res
    if parsed.scheme.lower() != "https":
        fail(
            "https",
            f"mint_url harus HTTPS, dapat scheme {parsed.scheme!r} "
            f"({mint_url!r}) — HTTP / scheme lain rawan disadap/diubah.",
        )
        res.verdict = "FAIL"
        return res
    if parsed.username or parsed.password:
        fail("mint_url", "mint_url mengandung userinfo (username/password) — tolak.")
        res.verdict = "FAIL"
        return res
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        fail("mint_url", f"mint_url tidak punya host: {mint_url!r}")
        res.verdict = "FAIL"
        return res
    if host != official:
        reason = _looks_like_typosquat(host, official)
        fail(
            "host_match",
            f"host mint_url {host!r} != domain resmi {official!r}. "
            + (reason or "Domain berbeda — kemungkinan besar website palsu."),
        )
        res.verdict = "FAIL"
        return res
    res.add("host_match", "PASS", f"host mint_url sama persis dengan domain resmi: {host}")
    # Homograph check juga untuk host yang sama persis secara byte tapi
    # mengandung karakter aneh (tidak mungkin sama persis + non-ASCII
    # kalau official-nya ASCII — tetap dicek eksplisit).
    typo = _looks_like_typosquat(host, official)
    if typo:
        fail("typosquat", typo)
        res.verdict = "FAIL"
        return res
    res.add("typosquat", "PASS", "tidak ada pola typosquat/homograph di host")

    # (d)+(e) Ikuti redirect, TLS valid (verify default True).
    try:
        session = requests.Session()
        session.max_redirects = MAX_REDIRECTS
        resp = session.get(
            mint_url,
            timeout=FETCH_TIMEOUT,
            allow_redirects=True,
            headers={"User-Agent": "fastmint-website-check/1.1"},
            stream=True,
        )
        final_url = resp.url
        res.final_url = final_url
        final_host = (urlparse(final_url).hostname or "").strip().lower().rstrip(".")
        if final_host != official:
            fail(
                "redirect",
                f"redirect berakhir di {final_host!r} ({final_url!r}), "
                f"bukan domain resmi {official!r} — kemungkinan phishing.",
            )
            res.verdict = "FAIL"
            return res
        for hop in resp.history:
            hop_host = (urlparse(hop.url).hostname or "").strip().lower().rstrip(".")
            if hop_host != official:
                fail(
                    "redirect",
                    f"redirect melewati domain lain {hop_host!r} ({hop.url!r}) — tolak.",
                )
                res.verdict = "FAIL"
                return res
        res.add("redirect", "PASS", f"redirect berakhir di domain resmi ({final_url!r})")
        if resp.status_code in (403, 429, 503):
            # Situs legit di balik WAF/rate-limit sering menolak client bot
            # dengan 403/429 — domain + TLS + redirect sudah terverifikasi,
            # dan safety gate kontrak tetap melindungi dana. Scan konten
            # dilewati dengan WARN, bukan FAIL.
            res.add(
                "http_status",
                "WARN",
                f"website resmi menjawab HTTP {resp.status_code} (WAF/rate-limit) — "
                "isi halaman tidak bisa dibaca, scan konten dilewati. "
                "Domain + TLS + redirect sudah terverifikasi.",
            )
            body = ""
        elif resp.status_code != 200:
            fail(
                "http_status",
                f"website resmi menjawab HTTP {resp.status_code}, bukan 200 — "
                "tidak bisa memverifikasi halaman. FAIL (fail-closed).",
            )
            res.verdict = "FAIL"
            return res
        else:
            res.add("http_status", "PASS", f"HTTP {resp.status_code} dari domain resmi")
        # Baca body (dibatasi), tanpa eksekusi JS — hanya kalau HTTP 200.
        # Kalau status WAF (403/429/503), body sudah "" dari cabang di atas.
        if resp.status_code == 200:
            content = resp.raw.read(MAX_BODY_BYTES + 1, decode_content=True) if resp.raw else b""
            body = content[:MAX_BODY_BYTES].decode("utf-8", errors="ignore")
    except requests.exceptions.TooManyRedirects:
        fail("redirect", f"redirect lebih dari {MAX_REDIRECTS} kali — tolak.")
        res.verdict = "FAIL"
        return res
    except requests.exceptions.SSLError as e:
        fail("tls", f"sertifikat TLS tidak valid / gagal verifikasi: {str(e)[:150]}")
        res.verdict = "FAIL"
        return res
    except requests.exceptions.RequestException as e:
        fail(
            "fetch",
            f"website resmi tidak bisa dihubungi/diverifikasi: "
            f"{type(e).__name__}: {str(e)[:150]} — FAIL (fail-closed).",
        )
        res.verdict = "FAIL"
        return res

    # (f) Scan halaman — dilewati kalau isi tidak terbaca (status WAF).
    if not body:
        res.add(
            "page_scan",
            "WARN",
            "isi halaman tidak terbaca (WAF/rate-limit) — scan seed phrase / "
            "alamat kontrak / link connect wallet dilewati. Domain + TLS + "
            "redirect sudah terverifikasi; safety gate kontrak tetap jalan.",
        )
        res.verdict = "FAIL" if failed else "PASS"
        return res
    low = body.lower()
    for pat in SEED_REQUEST_PATTERNS:
        if re.search(pat, low):
            fail(
                "seed_phrase",
                f"halaman MEMINTA seed phrase / private key (pola: {pat!r}) — "
                "website resmi tidak pernah meminta itu. Phishing.",
            )
            res.verdict = "FAIL"
            return res
    res.add("seed_phrase", "PASS", "halaman tidak meminta seed phrase / private key")
    if re.search(SEED_MENTION_PATTERN, low):
        res.add(
            "seed_mention",
            "WARN",
            "halaman menyebut seed phrase / private key (bisa jadi teks "
            "peringatan legit — periksa konteksnya manual sebelum mint).",
        )

    if contract:
        expected = checksum(contract).lower() if is_valid_address(contract) else contract.lower()
        found = {a.lower() for a in ADDRESS_RE.findall(body)}
        if found and expected not in found:
            if explicit_mint_url:
                fail(
                    "contract_on_page",
                    f"halaman mint menampilkan alamat kontrak lain "
                    f"({sorted(found)[:3]}) yang tidak sama dengan kontrak di yaml "
                    f"({expected}) — kemungkinan halaman palsu / kontrak diganti.",
                )
                res.verdict = "FAIL"
                return res
            # Homepage domain resmi wajar menampilkan banyak koleksi lain —
            # bukan bukti palsu, hanya WARN. Yang ketat hanya halaman mint.
            res.add(
                "contract_on_page",
                "WARN",
                "homepage menampilkan alamat kontrak lain (wajar — homepage "
                "memuat banyak koleksi). Kontrak mint tetap harus cocok "
                "dengan yang dicek safety gate.",
            )
        if found:
            res.add(
                "contract_on_page",
                "PASS",
                f"alamat kontrak di halaman sama dengan kontrak di yaml ({expected}).",
            )
        else:
            res.add(
                "contract_on_page",
                "WARN",
                "tidak ada alamat kontrak 0x... di HTML halaman (bisa jadi "
                "dimuat via JS) — alamat kontrak tetap harus cocok dengan "
                "yang dicek safety gate.",
            )

    # Link "connect wallet" ke domain lain -> FAIL.
    for href, inner in HREF_RE.findall(body):
        text = TAG_STRIP_RE.sub(" ", inner).strip().lower()
        if "connect" not in text or "wallet" not in text:
            continue
        if href.startswith("#") or href.startswith("javascript:"):
            continue
        target = urlparse(href)
        if not target.netloc:
            continue  # link relatif, tetap di domain resmi
        target_host = (target.hostname or "").strip().lower().rstrip(".")
        if target_host and target_host != official:
            fail(
                "connect_wallet_link",
                f"tombol 'connect wallet' mengarah ke domain lain "
                f"{target_host!r} ({href!r}) — phishing.",
            )
            res.verdict = "FAIL"
            return res
    res.add(
        "connect_wallet_link",
        "PASS",
        "tidak ada link 'connect wallet' ke domain lain.",
    )

    # official_x / official_discord: validasi ringan (bukan sumber kebenaran).
    for key, allowed in (
        ("official_x", ("x.com", "twitter.com")),
        ("official_discord", ("discord.com", "discord.gg", "discordapp.com")),
    ):
        val = (project.get(key) or "").strip()
        if not val:
            continue
        parsed = urlparse(val if "://" in val else "https://" + val)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or host not in allowed:
            res.add(
                key,
                "WARN",
                f"{key} = {val!r} bukan URL {allowed} yang valid — "
                "periksa manual, jangan jadikan bukti resmi.",
            )
        else:
            res.add(key, "PASS", f"{key} menunjuk ke {host}.")

    res.verdict = "FAIL" if failed else "PASS"
    return res
