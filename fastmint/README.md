# Fastmint WL NFT v1.1

Sistem mint NFT whitelist yang **cepat tapi tidak buta**: setiap mint harus
lolos website gate + safety gate + gas di bawah cap + simulasi `eth_call`
sebelum ada transaksi yang dikirim. Kalau satu saja gagal, sistem berhenti
dan melaporkan — tidak ada auto-retry broadcast.

> Status: v1.1 lokal, **belum di-push ke GitHub**. Folder ini nantinya masuk
> ke monorepo `supersmoney/supersmoney` sebagai folder `fastmint/`
> (satu repo, banyak project, masing-masing folder sendiri; root README
> repo tetap README profil). Push hanya setelah build + dry-run test lolos,
> dan **tidak pernah** membawa `keys/`, keystore, `.keystore-pass`, `.env`,
> seed, atau private key (lihat `.gitignore`).
>
> Yang baru di v1.1 (25 Nov 2026): website gate anti-phishing
> (`fastmint/website.py`) + perbaikan audit — cek harga on-chain vs tx
> value (tutup celah overpay), cek quantity vs max per wallet, scan source
> semua file project, `.gitignore` menahan `projects/*.yaml` (wl_proof),
> re-check gas/nonce/simulasi/harga sesaat sebelum signing live, dan
> verifikasi balanceOf sesudah mint. Detail di bawah.

## Cara pakai (untuk pengguna baru)

Repo ini template — semua yang berisi data pribadi **sudah dikosongkan**, kamu isi sendiri dengan panduan Muse AI kamu:

1. **Buat 1 wallet khusus mint** (jangan pakai wallet utama). Simpan keystore-nya di `keys/`, jangan pernah push `keys/` ke GitHub.
2. **Isi `config/chains.yaml`** — alamat wallet agent + wallet kamu di bagian `wallets`, dan batas gas per chain.
3. **Isi `projects/<nama>.yaml`** — salin dari `projects/example.yaml`, isi alamat kontrak + `official_website` + `mint_url` dari website resmi proyek (bukan dari DM).
4. **Jalankan dry-run dulu**: `python -m fastmint.check_mint --chain <chain> --contract <alamat> --mode dry-run --project projects/<nama>.yaml` — harus PASS semua gate sebelum mint.
5. **Baru mint** — mode otomatis (wallet agent) atau mode approve (wallet kamu approve sendiri di Phantom/browser). Jangan pernah kirim seed / private key ke siapa pun, termasuk ke AI.

## Wallet

| Wallet | Alamat | Pegang kunci | Dipakai untuk |
|---|---|---|---|
| Agent WL NFT | `<alamat-wallet-agent-kamu>` | Muse kamu (keystore terenkripsi di `keys/`, password di file terpisah, keduanya 0600) | Mode **otomatis** — hanya kalau WL terdaftar atas alamat ini |
| User (Phantom) | `<alamat-wallet-kamu>` | User (seed dipegang user) | Mode **approve** — sistem hanya menyiapkan tx, user approve sendiri. Sistem **tidak pernah** sign dengan wallet ini, dan tidak pernah minta seed / private key user. |

Satu alamat EVM berlaku di semua chain EVM. Yang dipisah per chain adalah
config gas + funding (token gas tiap chain beda, kepadatan user beda).

Aturan wallet (disepakati 25 Nov 2026): untuk **setiap** project baru,
tentukan dulu wallet mana yang dipakai (user vs agent) **sebelum** kerja
dimulai. WL yang sudah terdaftar pakai wallet user tetap pakai wallet user
(mode approve). WL baru yang mau mode otomatis didaftarkan pakai alamat
wallet agent.

## Alur per mint

```
1. PRE-MINT (jauh sebelum jam mint)
   - Dapat alamat kontrak HANYA dari sumber resmi (website / X / Discord
     resmi), cocokkan di minimal 2 sumber.
   - Buat projects/<nama>.yaml dari projects/example.yaml.
   - Isi chain, contract, quantity, mint_function, mint_price_wei,
     official_website, official_x, official_discord, mint_url.

0. WEBSITE GATE  (fastmint/website.py — fail-closed, jalan SEBELUM safety)
   - Project yaml WAJIB punya `official_website`. Tidak ada -> FAIL.
   - `mint_url` harus HTTPS dan host-nya SAMA PERSIS dengan domain resmi.
     Beda satu huruf / tambah hyphen / beda TLD / subdomain asing -> FAIL.
   - Typosquat/homograph: punycode (xn--), karakter non-ASCII mirip
     huruf Latin, edit-distance kecil dari domain resmi -> FAIL.
   - Redirect (max 5) harus berakhir di domain resmi; TLS harus valid.
   - Halaman (tanpa eksekusi JS): minta seed phrase / private key -> FAIL.
     Alamat kontrak di halaman mint harus SAMA dengan kontrak di yaml.
     Link "connect wallet" ke domain lain -> FAIL.
   - Tanpa project yaml (CLI langsung --chain/--contract): gate SKIP dan
     dicatat — mint langsung ke kontrak tanpa lewat website apa pun
     (jalur paling aman dari phishing website).
   - Catatan: cek kontrak-di-halaman hanya ketat untuk `mint_url`
     eksplisit. Homepage domain resmi wajar memuat banyak koleksi lain,
     jadi di sana hanya WARN. Jangan isi homepage sebagai `mint_url` —
     isi URL halaman mint / koleksinya.
   - Status HTTP 403/429/503 (WAF/rate-limit): scan konten dilewati
     dengan WARN — domain + TLS + redirect sudah terverifikasi, dan
     safety gate kontrak tetap melindungi dana.

2. SAFETY GATE  (fastmint/safety.py — fail-closed)
   - Alamat valid, chain dikenal, RPC terkoneksi.
   - eth_getCode: harus ada bytecode (EOA / alamat kosong -> FAIL).
   - Kontrak terverifikasi di Sourcify v2 (tanpa API key).
     Etherscan V1 sudah deprecated, V2 butuh API key — jadi Sourcify
     adalah sumber verifikasi utama v1. Tidak terverifikasi -> FAIL.
   - ABI harus punya fungsi mint. approve / setApprovalForAll /
     transferFrom yang ada di ABI dicatat sebagai INFO (standar ERC721),
     bukan bukti drain.
   - Scan source SEMUA file source project (bukan hanya file utama):
     selfdestruct / delegatecall / approval di dalam fungsi mint /
     fungsi drain -> FAIL. (v1.1: v1 hanya scan file utama, drain di
     file import lolos.)
   - Proxy EIP-1967: implementation harus ikut terverifikasi.
   - Baca mint params on-chain: totalSupply, maxSupply, harga, max per
     wallet, status sale, owner, name, symbol.
   - Kalau satu langkah tidak bisa diverifikasi -> FAIL, bukan "coba saja".

2b. HARGA + QUANTITY  (fastmint/executor.py — audit v1.1)
   - Harga mint on-chain (mintPrice/price/cost/... atau saleConfig[2]
     gaya Azuki) x quantity harus SAMA PERSIS dengan tx value.
     Overpay -> DITOLAK (v1 tidak mengecek ini: kontrak dengan
     `msg.value >= price` tanpa refund akan menerima kelebihan bayar
     dan simulasi tetap lolos). Underpay -> DITOLAK (akan revert).
   - Quantity harus >= 1 dan tidak melebihi max per wallet on-chain.
   - Kalau harga on-chain tidak terbaca: WARN saja (simulasi tetap
     menangkap underpay; overpay jadi risiko yang dicatat).
   - Bug v1 yang diperbaiki: `--value-wei` CLI meng-override harga
     free mint (mint_price_wei=0) — sekarang harga di yaml selalu menang.

3. GAS  (fastmint/gas.py — baca LIVE dari RPC)
   - base fee + priority fee dari eth_feeHistory (fallback
     eth_maxPriorityFeePerGas / eth_gasPrice).
   - 3 tier: Hemat (masuk 2-3 blok, murah) / Seimbang / Cepat
     (kejar blok berikutnya, priority fee lebih tinggi).
   - Estimasi total = maxFeePerGas x gas limit. Kalau di atas
     max_total_fee_native di config/chains.yaml -> JANGAN KIRIM.
   - Nilai cap di config adalah PLACEHOLDER konservatif, bukan harga
     live — wajib dicek/disetujui user sebelum mint pertama tiap chain.

4. EKSEKUSI  (fastmint/executor.py — dry-run default)
   - Mode dry-run: cek + simulasi saja.
   - Mode approve: siapkan unsigned tx (to, data, value, gas, chain)
     untuk wallet user. User approve di Phantom/browser.
   - Mode auto: siapkan tx untuk wallet agent. Broadcast HANYA kalau
     --live + website PASS/SKIP + safety PASS + tidak ada blocking
     warning + gas di bawah cap + user mengetik MINT saat konfirmasi
     (ringkasan 1 baris: chain · from · to · asset · amount · spender ·
     estimasi fee).
   - Simulasi eth_call + eth_estimateGas dulu. Revert -> STOP.
   - Nonce diambil dari "pending" (bukan "latest") supaya tx di mempool
     ikut dihitung dan tidak bentrok.
   - Sesaat SEBELUM signing live, semua dicek ulang (audit v1.1):
     gas segar + cek cap lagi, nonce segar, simulasi ulang, harga +
     batas per wallet dibaca ulang, balanceOf sebelum mint. Kalau ada
     yang berubah (mis. sale sudah habis) -> BATAL tanpa broadcast,
     tidak ada gas hangus.

5. VERIFIKASI ON-CHAIN
   - Tunggu receipt, cek status == 1.
   - Cek balanceOf(wallet) sebelum vs sesudah mint (v1.1: fungsi ini
     sudah ada di v1 tapi tidak pernah dipanggil di jalur live —
     sekarang dipanggil; kalau balance tidak bertambah padahal status 1,
     hasil dilaporkan GAGAL verifikasi, jangan dianggap berhasil).
   - Klaim "berhasil" hanya sah kalau saldo NFT bertambah / tx
     terkonfirmasi on-chain.
   - Semua hasil dicatat ke logs/mint-checks.jsonl (JSONL, tanpa secret).
```

## Cara pakai

Dari `~/workspace/wl-nft`, pakai venv airdrop-agent (web3, eth_account,
yaml, requests sudah ada di sana):

```bash
cd ~/workspace/wl-nft
PY=~/workspace/airdrop-agent/.venv/bin/python

# 1. Cek kontrak (dry-run, tanpa sign, tanpa kirim dana)
$PY -m fastmint.check_mint --chain ethereum --contract 0x... --mode dry-run --tier seimbang

# 2. Dari file project
$PY -m fastmint.check_mint --project projects/nama-project.yaml --mode dry-run --tier hemat

# 3. Siapkan tx untuk di-approve user (wallet user)
$PY -m fastmint.check_mint --project projects/nama-project.yaml --mode approve --tier cepat

# 4. Auto + live (wallet agent saja; tetap minta ketik MINT)
$PY -m fastmint.check_mint --project projects/nama-project.yaml --mode auto --tier cepat --live
```

Exit code `check_mint`: `0` = READY, `2` = website FAIL / safety FAIL /
gas di atas cap / build gagal / harga tidak cocok (jangan mint), `3` =
simulasi revert (kontrak aman tapi mint tidak bisa jalan sekarang,
mis. sale belum buka / sold out / tidak eligible).

## File

| File | Isi |
|---|---|
| `config/chains.yaml` | Profil per chain: chainId, RPC publik, explorer, token gas, tier gas, max cap. Wallet (alamat saja, tanpa secret). |
| `fastmint/common.py` | Load config, alias chain, koneksi RPC (coba RPC berurutan, cek chain_id). |
| `fastmint/website.py` | **v1.1** Website gate anti-phishing: domain resmi, typosquat/homograph, redirect, TLS, scan seed phrase / kontrak di halaman / link connect wallet. Fail-closed. |
| `fastmint/safety.py` | Checker kontrak: verified Sourcify, bytecode, scan source (v1.1: semua file project), proxy, mint params. Fail-closed. |
| `fastmint/gas.py` | Strategi gas live: feeHistory, 3 tier, total estimasi vs cap. |
| `fastmint/executor.py` | Build + simulasi + broadcast live (auto saja, pakai keystore agent). Approve mode hanya mengeluarkan unsigned tx. v1.1: cek harga + quantity, re-check sebelum signing, verifikasi balanceOf. |
| `fastmint/check_mint.py` | CLI: website + safety + gas + simulasi, cetak PASS/FAIL, tulis log JSONL. |
| `projects/example.yaml` | Template per-mint (tanpa alamat kontrak asli). v1.1: + `official_website` / `official_x` / `official_discord` / `mint_url`. |
| `tests/test_v11.py` | **v1.1** Uji website gate + audit (30 cek, tanpa broadcast). |
| `tests/test_anvil_v11.py` | **v1.1** Uji E2E Anvil: overpay, TOCTOU abort, happy path + verifikasi balance. |
| `logs/` | Hasil cek/mint (`mint-checks.jsonl`). Format per baris ada di bawah. |
| `keys/` | Keystore wallet agent (0600). **Tidak pernah di-push.** |

## Format log (`logs/mint-checks.jsonl`)

Satu baris JSON per pengecekan / eksekusi, tanpa secret:

```json
{"ts": "2026-11-25T03:51:00+00:00", "chain": "ethereum",
 "contract": "0xED5AF388653567Af2F388E6224dC7C4b3241C544",
 "mode": "dry-run", "tier": "seimbang", "ready": false,
 "reason": "SIMULASI GAGAL (revert) — ...",
 "safety_verdict": "PASS", "gas": {...},
 "simulation_ok": false, "tx_hash": null}
```

## Yang TIDAK dilakukan sistem ini (v1.1)

- Tidak pernah broadcast mainnet tanpa `--live` + konfirmasi ketik MINT.
- Tidak pernah sign dengan wallet user, tidak pernah minta seed/private key.
- Tidak pernah set approval unlimited — executor hanya memanggil fungsi
  mint, tidak pernah membuat transaksi `approve` / `setApprovalForAll`.
  (Audit v1.1: diverifikasi di `executor.py` — tidak ada kode approval.)
- Tidak pernah bridge/swap/stake "sisanya".
- Tidak ada harga USD (belum ada price feed) — cap dihitung dalam token
  gas native per chain.
- Website gate TIDAK mengeksekusi JavaScript dan TIDAK menilai apakah
  project-nya bagus / legit secara bisnis — hanya memastikan URL-nya
  domain resmi dan halamannya tidak minta seed / kontrak beda. Website
  resmi dari project scam tetap bisa lolos gate website; safety gate
  kontrak + simulasi yang menjadi lapisan berikutnya. Selalu cocokkan
  domain resmi dari X / Discord resmi, jangan dari DM.
- Chain `robinhood` dan `litvm` di config masih `verification: manual` —
  safety akan FAIL untuk chain itu sampai ada sumber verifikasi otomatis
  yang bisa diuji (Sourcify belum tentu mendukung chain-nya / RPC-nya
  masih placeholder). Jangan dipaksa PASS manual.
- Etherscan API butuh API key untuk V2 — kalau suatu saat perlu
  getsourcecode Etherscan, isi `ETHERSCAN_API_KEY` via env, jangan tulis
  di file.
- `projects/*.yaml` per-mint TIDAK ikut di-push (`.gitignore` v1.1) —
  file itu bisa berisi `wl_proof` / signature WL. Hanya
  `projects/example.yaml` yang boleh ikut.

## Audit keamanan v1.1 (25 Nov 2026) — yang dicek & hasilnya

| Celah yang dicari | Hasil | Bukti / perbaikan |
|---|---|---|
| Website phishing (typosquat, redirect, minta seed) | **CELAH — diperbaiki** | `fastmint/website.py` baru, gate sebelum safety. Uji: opensea.io PASS, opensea-io.com / xn-- / http / tanpa official_website -> FAIL. |
| Overpay: tx value > harga on-chain | **CELAH — diperbaiki** | `check_price_and_quantity()`: harga on-chain x qty harus = tx value. Bukti Anvil: simulasi overpay 0.05 ETH untuk harga 0.01 ETH LULUS (v1 READY), v1.1 menolak OVERPAY. |
| Quantity > max per wallet | **CELAH — diperbaiki** | Cek yang sama; Azuki qty 6 > max 5 ditolak sebelum build. |
| `--value-wei` override free mint | **BUG — diperbaiki** | Harga di yaml (termasuk 0) sekarang selalu menang atas argumen CLI. |
| Drain di file source import (bukan file utama) | **CELAH — diperbaiki** | `collect_project_sources()` + scan semua file project. Bukti unit test: drain di file kedua tidak ketemu v1, ketemu v1.1. |
| `wl_proof` di project yaml ter-push ke repo publik | **CELAH — diperbaiki** | `.gitignore`: `projects/*.yaml` + `!projects/example.yaml`. Bukti `git check-ignore`. |
| TOCTOU: keadaan berubah antara prepare & live | **CELAH — diperbaiki** | `execute_live` re-cek gas/nonce/simulasi/harga sebelum signing. Bukti Anvil: supply habis sebelum live -> BATAL tanpa broadcast, saldo agent utuh. |
| Verifikasi balance sesudah mint tidak pernah dipanggil | **CELAH — diperbaiki** | `execute_live` sekarang memanggil `verify_mint_onchain`. Bukti Anvil: balance 0 -> 1, `mint_verified: true`. |
| Approval unlimited | OK — tidak ada | Executor tidak pernah membuat tx approval. |
| RPC tanpa cek chain_id / replay antar chain | OK — sudah ada | `connect_chain` cocokkan chain_id; tx bawa `chainId` dari config. Nonce sekarang dari "pending". |
| Secret bocor di log | OK — bersih | `grep` log `mint-checks.jsonl`: tidak ada private key / seed / password. Log tetap di-`.gitignore`. |
| Harga on-chain tidak terbaca (mis. layout saleConfig lain) | Batasan dicatat | Hanya WARN (bukan blokir) — simulasi tetap menangkap underpay; risiko overpay untuk layout tak dikenal dicatat di README ini. |

## Uji v1 (25 Nov 2026)

- **Azuki `0xED5AF388653567Af2F388E6224dC7C4b3241C544` (Ethereum):**
  safety **PASS** (Sourcify `exact_match`, bytecode 14.251 bytes, bukan
  proxy, fungsi mint `allowlistMint` / `publicSaleMint` / `auctionMint`
  ada, 6 mint param terbaca: `name`, `symbol`, `totalSupply` 10.000,
  `saleConfig`, `maxPerAddressDuringMint` 5, `owner`).
  Simulasi mint revert dengan alasan `not eligible for allowlist mint` —
  benar dan diharapkan (sale sudah lewat / wallet tidak eligible).
  Jadi verdict keseluruhan NOT READY (exit 3), tapi safety checker-nya
  terbukti bekerja: kontrak aman dikenali, dan mint yang tidak bisa
  jalan ditolak sebelum ada dana bergerak.
- **Alamat palsu** (`0x0000...0000`, wallet agent sendiri yang belum
  punya code, dan template `projects/example.yaml`): safety **FAIL**
  (tidak ada bytecode, exit 2).
- **Gas live:** Base + BSC + Ethereum (drpc) terbaca via `eth_feeHistory`;
  tier hemat vs cepat beda nyata (mis. Ethereum cepat maxFee ~3,06 gwei
  vs hemat ~0,05 gwei saat sepi), semua di bawah cap saat pengujian.
- **E2E lokal di Anvil (chain 31337, tanpa dana asli):** kontrak
  `TestMint` (ERC721 minimal, free mint, `mint(uint256)`) di-deploy ke
  Anvil; jalur executor penuh diuji — build tx auto + simulasi OK
  (`estimate_gas` 116.575), build tx approve unsigned (from = wallet
  user), sign dengan keystore agent **asli** + broadcast ke Anvil,
  receipt status 1, verifikasi on-chain `balanceOf` 0 → 1. **LULUS.**
  (Safety gate tidak diuji di Anvil karena Anvil tidak ada di Sourcify —
  safety sudah diuji terpisah di Azuki + alamat palsu di atas.)
- **Tidak ada broadcast mainnet, tidak ada dana yang dikirim, wallet
  agent tidak diisi gas** selama pengujian ini. Satu-satunya broadcast
  adalah ke Anvil lokal dengan test-ETH.

## Uji v1.1 (25 Nov 2026)

- **`tests/test_v11.py` — 30/30 lolos** (tanpa broadcast, tanpa dana):
  website gate live `opensea.io` PASS; typosquat `opensea-io.com`,
  punycode `xn--`, `http://`, dan project tanpa `official_website`
  -> FAIL; tanpa project yaml -> SKIP; halaman minta seed phrase,
  kontrak di halaman beda, link connect wallet ke domain lain, dan
  redirect lewat domain lain -> FAIL (mock); harga Azuki terbaca dari
  `saleConfig[2]` = 0.5 ETH; qty 1 value 0 / 1 ETH / qty 6 > max 5 /
  quantity 0 -> ditolak; scan source: drain di file import tidak
  ketemu v1, ketemu v1.1; `.gitignore` menahan `projects/*.yaml`
  (bukti `git check-ignore`).
- **`tests/test_anvil_v11.py` — 6/6 lolos** (Anvil lokal, forge build,
  tanpa dana asli): (A) simulasi overpay 0.05 ETH untuk harga 0.01 ETH
  LULUS — membuktikan celah v1 — lalu cek harga v1.1 menolak OVERPAY;
  (B) supply habis dibeli orang lain sebelum live -> `execute_live`
  BATAL tanpa broadcast, saldo agent utuh; (C) happy path live OK,
  receipt status 1, balance 0 -> 1, `mint_verified: true`.
- **Regresi v1 — tetap seperti sebelumnya, dengan satu perbaikan
  perilaku yang disengaja:** Azuki dry-run CLI langsung: WEBSITE SKIP,
  SAFETY PASS, hasil NOT READY exit 3 — dulu karena simulasi revert,
  sekarang ditangkap lebih awal sebagai HARGA TIDAK COCOK (underpay:
  harga on-chain 0.5 ETH, tx value 0). Alamat palsu: SAFETY FAIL
  exit 2. Template `example.yaml`: WEBSITE FAIL exit 2 (tanpa
  `official_website`). Project Azuki + `mint_url`
  `https://opensea.io/collection/azuki`: WEBSITE PASS (semua cek,
  termasuk alamat kontrak di halaman cocok) -> SAFETY PASS -> exit 3
  (harga). Catatan: `mint_url` homepage `https://opensea.io/` sebagai
  URL mint eksplisit akan FAIL di cek kontrak-di-halaman karena homepage
  memuat banyak koleksi lain — itu benar secara desain; isi URL halaman
  mint / koleksinya, bukan homepage.
- **Tidak ada broadcast mainnet, tidak ada dana dikirim, wallet agent
  tidak diisi gas** selama pengujian ini. Satu-satunya broadcast adalah
  ke Anvil lokal (test-ETH) dan pendanaan wallet agent 1 test-ETH di
  Anvil saja (bukan mainnet). Log dicek bersih dari secret.

## Keputusan yang masih butuh user

- Batas gas final per chain (`max_total_fee_native` di config masih
  placeholder konservatif).
- RPC API key kalau ada (opsional — RPC publik di config sudah cukup
  untuk v1.1, tapi bisa rate-limit saat mint ramai).
- Project WL pertama yang mau dicoba fastmint ini (isi
  `projects/<nama>.yaml` dari sumber resmi — ingat isi
  `official_website` + `mint_url`-nya).
- Push ke GitHub: masih lokal, belum di-push. (Catatan: push v1 sempat
  gagal 403 karena instalasi GitHub App belum mencakup repo — itu
  urusan user di pengaturan GitHub, bukan urusan kode di sini.)
