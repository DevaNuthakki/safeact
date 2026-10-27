# Development evidence held outside the repository

Large raw development packets are not committed. They live in `~/safeact-evidence/` on the author's machine, at the same relative paths they had in this repository (for example `~/safeact-evidence/docs/phase-e/dev-recovery-20261022/raw.jsonl`).

What stays in the repository for every packet: its `REVIEW.md`/`RESULTS.md`, plans, summaries, protocols, manifests, small check and replay files, and its digest files (`artifact-sha256.json`, `source-sha256.json`). Files read by code or tests, or linked from documents outside their packet, also stay (the two `results.jsonl` files).

What moved: source snapshots (`*.tar.gz`), `raw/`, `regressions/`, `diagnostics/`, `logs/`, raw `*.jsonl` samples, provider ledgers, logs, screenshots, and any other file over 64 KiB. Links in a packet's sealed `REVIEW.md` to these files are intentionally left unchanged; they resolve under `~/safeact-evidence/`.

These are development packets only. The Stripe sandbox raw results are committed with their run. The full qualification run follows the storage decision below.

## Full qualification run: storage decision (author, 2026-10-25)

The full run's raw output (estimated at about 1 GB) is not committed to git.

**Committed with the run:**
- the report and summaries;
- every failure row: any safety failure, error, not-run, not-applicable-with-error or useful-completion miss, in any arm;
- the coverage table and the environment records;
- interleaving traces (the raw rows) for every failing row and every row unstable at scoring;
- SHA-256 digests of both the raw files and their compressed copies.

**Outside git:** the compressed raw output is kept in `~/safeact-evidence/docs/phase-e/qual-v1/full/`. It will be published as a GitHub Release asset on the public repository and linked from the README. The committed digests let anyone verify the downloaded asset and the raw files it contains.

The calibration raw files follow the same rule; their digests are in `docs/phase-e/qual-v1/calibration/outside-repo.json`.

## Verifying

- Each moved file's SHA-256 is in [evidence-moved-sha256.json](evidence-moved-sha256.json) and also in its packet's committed `artifact-sha256.json`, which every packet passed before the move (2026-10-24: 583 entries, 0 mismatches).
- The packet digest below is the SHA-256 of that packet's lines `<sha256>  <path>\n`, sorted by path, the same format `shasum -a 256` prints:

```sh
cd ~/safeact-evidence && find docs/phase-e/dev-recovery-20261022 -type f | sort | xargs shasum -a 256 | shasum -a 256
```

## Packets

| Packet | Moved files | Bytes | Packet digest |
|---|---:|---:|---|
| `docs/demo/validation-20261022` | 25 | 1,676,030 | `sha256:69527455256becdbfdb10542b19af127891f700bf6e33e255052318507d4048c` |
| `docs/phase-e/dev-baseline-adapter-20261023` | 86 | 8,462,874 | `sha256:acc6bba66a00c3400d8cb49976489515ba07a0689bfb1756218d5cc8df287793` |
| `docs/phase-e/dev-baseline-execution-20261023` | 34 | 1,325,874 | `sha256:7979e8b072874c46c0f9d4246e75e1a32733a7c2d61bc4862decae3e8f9b340c` |
| `docs/phase-e/dev-baseline-observer-20261023` | 24 | 1,045,361 | `sha256:e44fb049641aa9d8341d94ca94249b792a336229d4530120bae9192db373a297` |
| `docs/phase-e/dev-baseline-owned-stack-20261023` | 46 | 1,989,250 | `sha256:e0042e96b9988f92013a4e5398e7329770db6c8b7db9b7c3edb31d8e6402c3de` |
| `docs/phase-e/dev-baseline-process-trace-20261023` | 33 | 1,056,234 | `sha256:f1ebad94aa817e7c0a5987eb2611059d8b93c81b3232e1b1c3e8ab853e506b2d` |
| `docs/phase-e/dev-baseline-workflow-review-20261023` | 35 | 2,184,932 | `sha256:7f9b755674276f86b5dee45d835b7a286d330829e1bbcd0d92c67d0d8ad1271f` |
| `docs/phase-e/dev-boundaries-20261022` | 49 | 10,374,261 | `sha256:1fc5a52ac557f0d6f99a8f01ffa45308abda95a5040cd4313b53ec84723056d0` |
| `docs/phase-e/dev-pooling-integration-20261024` | 35 | 2,359,574 | `sha256:5d1ab9e6212e72f8ba7c71aa7ff002c71e0bb8f671019c9092a5a964e080807d` |
| `docs/phase-e/dev-recovery-20261022` | 1 | 2,428,042 | `sha256:3c83813c86066f59c4a5fd81a97791b7da8db3d8765f90d26eeac23986404379` |
| `docs/phase-e/dev-workflows-20261022` | 12 | 927,733 | `sha256:7b867ab6456a43293aa28ca1ec04ba9a17eb65f4170bff1454201987ead906ca` |
| `docs/phase-e/local-pooling-experiment-20261024` | 3 | 1,401,746 | `sha256:a1dcf3612fdae89542c97b70e1f706daa7d4685f24db08be5469dbfa009958ee` |
| `docs/phase-e/local-risk-check-20261024` | 4 | 562,459 | `sha256:35443031be2762eb53b5d53c8b068e71509d25976dc14ba287ac734581644412` |
| **Total** | **387** | **35,794,370** | |

## Tarballs

| File | Bytes | Digest |
|---|---:|---|
| `docs/demo/validation-20261022/source.tar.gz` | 680,009 | `sha256:56d40be6c8d3a13ab5683cfe4cd3a9f5ff33b5c42459aeb9cc23e15e1bcdce70` |
| `docs/phase-e/dev-baseline-adapter-20261023/development-runs.tar.gz` | 6,549,542 | `sha256:d925b813aff9d91818de724bbc86f7a7b97994599f93143db044c44b40d278f9` |
| `docs/phase-e/dev-baseline-adapter-20261023/focused-captures.tar.gz` | 186,357 | `sha256:ecf7e8453b84489ad37465853efb27bd96113e07caaa94572dc9854ea26ad7ee` |
| `docs/phase-e/dev-baseline-adapter-20261023/source.tar.gz` | 916,039 | `sha256:8e231c808c601abf5303a520249e64741205dfd19f1ab8091f3701ca7448f377` |
| `docs/phase-e/dev-baseline-execution-20261023/source.tar.gz` | 701,775 | `sha256:a83fa0168090dd5afd62631f8751c0bf7370e7149e0fd30d223a3064b82a7b0d` |
| `docs/phase-e/dev-baseline-observer-20261023/source.tar.gz` | 747,393 | `sha256:83226074dfb10c5167748a7f744c66a96fc673d3dd71b4e2e458b8df821a8dab` |
| `docs/phase-e/dev-baseline-owned-stack-20261023/initial-development.tar.gz` | 48,526 | `sha256:9903cde64d0fdffba693a5632f77debf4973a17898fe6bd216891a46d9e1cf4f` |
| `docs/phase-e/dev-baseline-owned-stack-20261023/source.tar.gz` | 769,279 | `sha256:a41118e283235e8650f2715f72561fd937b53970afbd0fb117b42e58edb34301` |
| `docs/phase-e/dev-baseline-process-trace-20261023/source.tar.gz` | 757,921 | `sha256:ceaccf199375a1957a7df00b7b3c96ed05c752cbe131289a91b1a254db1f9f53` |
| `docs/phase-e/dev-baseline-workflow-review-20261023/source.tar.gz` | 718,701 | `sha256:37971781fe5a1dfde234118eaba105152e7369acbeb3a9fa1bdfedf4adc5c5be` |
| `docs/phase-e/dev-boundaries-20261022/qualification-source.tar.gz` | 674,254 | `sha256:4a174992c3be4352254d98ac7758c0a800c82862fdd9af01f5a9c194738f6acd` |
| `docs/phase-e/dev-boundaries-20261022/source.tar.gz` | 683,225 | `sha256:f389e4c0efc46abccfaa0d67a9b68039ac040d8a8f1f6634a6a5a296949b02c2` |
| `docs/phase-e/dev-pooling-integration-20261024/source.tar.gz` | 926,290 | `sha256:0018a8c8701634f685f9a21689abd98f29c2fa18f8af529acc288d0d7d25376a` |
| `docs/phase-e/dev-workflows-20261022/source.tar.gz` | 244,337 | `sha256:603b210655b584a9862238383aca20726ca781c20c461d699a6a5f5e49e80f17` |

## Later packets

| Packet | Outside the repo | Contents |
|---|---|---|
| `docs/phase-e/stop2-post-pooling-20261024` | `regressions/` (5 logs) | Final regression logs on commit `fb89604`; per-log digests are in the packet's `regression-checks.json` |
| `docs/phase-e/dev-rerun-post-pooling-20261024` | `raw.jsonl` (66,030,228 bytes, `sha256:bff3439a32b8a02f293296adebeafb827a16f4818bdc153df20e355c1dc9519f`), `qualify-dev.log` (`sha256:bb72851ab4297b566114e5fcde44aa8cb94494d02debc9059cd3d99fb611ef9c`) | Post-pooling development rerun on `fb89604`; digests also in the packet's `outside-repo.json` |
| `docs/phase-e/dev-rerun-f05-f06-f16-20261024` | `raw.jsonl` (37,996,572 bytes, `sha256:832c820c3a94ec4a82f6fd742b9fd762a8675c3f8688e821914576f69200e29e`), `qualify-dev.log` (`sha256:cf62e3506655789385e87fa9c2e94a5895b574bda4de1e7b172973fc6c20ca40`) | F05/F06/F16 development rerun on `a485860`; digests also in the packet's `outside-repo.json`; full-suite logs in `regressions/`, digests in the packet's `regression-checks.json` |
| `docs/phase-e/dev-rerun-snapshot-20261024` | `raw.jsonl` (70,936,626 bytes, `sha256:6b1a6211d2b5b873dce6db87bed39caebcfa771f7e32362368b55f77ef209af0`), `qualify-dev.log` (`sha256:ee964f86c3ad5c4f4d8af29fb73c4eef76790066886541fb2bfd835355fed709`) | Full development rerun on `c55dbae` (snapshot fix); digests also in the packet's `outside-repo.json` |
| `docs/phase-e/qual-v1/calibration` | `slots-1/raw.jsonl` (`sha256:7311760568fa418d161c9bcdcc4fe96fd3c435916d7a85963f5418fca08b1ba3`), `slots-2/raw.jsonl` (`sha256:e3aa1599f79a5bd755168b50bc75cb1ed56009c8a770de81d22cbb8d04273900`), `slots-4/raw.jsonl` (`sha256:907daf029f6b84fd47040fb016cceb771d3bb5fc4fe50542419d4a2ace11ad52`), `slots-6/raw.jsonl` (`sha256:773b8dd1f33c011bbd7d8f419c0335bbc215a004debc2c2ec70d1ae54b61677c`), `slots-7/raw.jsonl` (`sha256:fd4bf71730de20b6a6a9efad0eb5ac521e3d1dbd6ab65e2ddc84d5b22a14ec94`); per-run logs and the sequence log | Frozen slot calibration (manifest `b779fa72`), selected 4 slots; raw files also stay in the working tree (gitignored) for the full run's digest check; all digests in the packet's `outside-repo.json` |
| `docs/phase-e/qual-v1/full` | `raw.jsonl` (1,196,365,366 bytes, `sha256:5faff691852d76a4d5467d325c8e01a21720a2af095f6e4f9e2df2645b63f710`), `raw.jsonl.gz` (55,115,769 bytes, `sha256:9ef815c22a9e3d90d504cd459996de2770f3aad8fe491dd2bb2ca35444707d1e`), `qualify-full.log` (284,916 bytes, `sha256:4892910ff8d7d3584f720b72a2016e711136ec4cd4c3c1006052e35e3e4e2fd2`) | Full qualification run v1 (primary). `raw.jsonl` stays in the working tree (gitignored); `raw.jsonl.gz` is in ~/safeact-evidence and is the planned GitHub Release asset (not yet published); committed: report, summaries, all scored rows, failure rows, traces of failing/unstable rows |
| `docs/phase-e/qual-v1/diagnostics` | `raw.jsonl` (48,127,403 bytes, `sha256:4c248f3dd12f85ed50d4b3405210c610d68e82198f86e29fbbf535f8429ee1b1`), `raw.jsonl.gz` (3,039,654 bytes, `sha256:eeb3a3905eaf33c32e185e23b5fa2ad6ce355a1f0180274430912b989120f719`), `qualify-diagnostic.log` (27,900 bytes, `sha256:e6bed498b77a2ae134f4866f7429486194486e92504209231d76723982e7e0d3`) | Load-suspect diagnostic rerun (118 scenarios x 5 arms, 1 slot), beside the headline; `raw.jsonl` stays in the working tree (gitignored), `raw.jsonl.gz` in ~/safeact-evidence; committed: report, summaries, all 590 scored rows, comparison with the primary rows |
| `docs/phase-e/load-v1-run1` (whole packet) | 7 files: `artifact-sha256.json`, `config.json`, `environment.json`, `requests.jsonl.gz` (`sha256:47dc02b2e3fead0d1d56494ef662d4af3fb6110b924b5d5123a0794f588a0f6c`), `run.log` (`sha256:e4b50ac4990934b218b866fd7f89de51e6330eda11cb01b1c0632e3e19caebbe`), `samples.jsonl.gz` (`sha256:0d4dad5ad647a48250ab9df0f831ea881a0028d817aab364e277e6c7df426441`), `summary.json` (`sha256:ea51f228ac1212b9aa0bcc8834537c8b8dd342adfb0964ef62a8ed16f9da7082`) | First M5.13 load run (2026-10-25 20:01-20:11 UTC), superseded because it reported accepted/s over the arrival window; same safety results; the committed run is `docs/phase-e/load-v1` |
| `docs/phase-e/compose-v1` | `fresh-clone-60c7f35-warm.log` (25,171 bytes, `sha256:79688e4e516a33289590bd617c92dcb8ce8af16d103758a568e43fc089901d04`), `fresh-clone-5ee1a8d-cold.log` (44,442 bytes, `sha256:6ca16874599fffda06d8ba60496b6a53bb28aa40d19d2b9f185d9bacf5bc749c`) | Fresh-clone check logs (M5.14), both PASS; digests also in the packet's `artifact-sha256.json` |

## Console screenshots

| Folder in ~/safeact-evidence | Files | Bytes | Digests |
|---|---:|---:|---|
| `console-polish-2/` | 46 | 5284956 | `SHA256SUMS` in the folder lists every file (`sha256:d2387e84f6732aee3d4d750edbcc42350f031a18b5cf408298124c661290a9bf`); `compare.html` `sha256:76f9c06325f102df4caec757639ba1fe2ccbb55a8c566625d4c0774e63f4f6c6` |

Console polish round 2 (2026-10-26), taken on the rebuilt and re-seeded Compose stack:
`light/` every screen (18), `dark/` the main screens (8), `round-1/` the round-1 Compose
screenshots copied for comparison (its workflow screen is from round 1's dev-stack run), and
`compare.html`, a side-by-side page (round 1 left, round 2 right; screens new in round 2 say so).
Verify with `cd ~/safeact-evidence/console-polish-2 && shasum -a 256 -c SHA256SUMS`.
