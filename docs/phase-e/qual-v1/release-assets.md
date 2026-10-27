# Qualification v1 release assets

The raw qualification output is committed by digest only. Three files are published
as assets of the [v1.0.0 release](https://github.com/DevaNuthakki/safeact/releases/tag/v1.0.0)
(2026-10-27, tag on `a25bee2`). Each asset has the same bytes as the file named in a
sealed packet; only the file name differs, so that the names are unique within the release.

| Release asset | Bytes | SHA-256 | Sealed entry |
|---|---:|---|---|
| [`qual-v1-full-raw.jsonl.gz`](https://github.com/DevaNuthakki/safeact/releases/download/v1.0.0/qual-v1-full-raw.jsonl.gz) | 55,115,769 | `9ef815c22a9e3d90d504cd459996de2770f3aad8fe491dd2bb2ca35444707d1e` | [full/outside-repo.json](full/outside-repo.json): `raw.jsonl.gz` |
| [`qual-v1-full-qualify.log`](https://github.com/DevaNuthakki/safeact/releases/download/v1.0.0/qual-v1-full-qualify.log) | 284,916 | `4892910ff8d7d3584f720b72a2016e711136ec4cd4c3c1006052e35e3e4e2fd2` | [full/outside-repo.json](full/outside-repo.json): `qualify-full.log` |
| [`qual-v1-diagnostics-raw.jsonl.gz`](https://github.com/DevaNuthakki/safeact/releases/download/v1.0.0/qual-v1-diagnostics-raw.jsonl.gz) | 3,039,654 | `eeb3a3905eaf33c32e185e23b5fa2ad6ce355a1f0180274430912b989120f719` | [diagnostics/outside-repo.json](diagnostics/outside-repo.json): `raw.jsonl.gz` |

Verify:

```sh
shasum -a 256 qual-v1-full-raw.jsonl.gz qual-v1-full-qualify.log qual-v1-diagnostics-raw.jsonl.gz
gunzip -c qual-v1-full-raw.jsonl.gz        | shasum -a 256   # 5faff691852d76a4d5467d325c8e01a21720a2af095f6e4f9e2df2645b63f710 (raw.jsonl)
gunzip -c qual-v1-diagnostics-raw.jsonl.gz | shasum -a 256   # 4c248f3dd12f85ed50d4b3405210c610d68e82198f86e29fbbf535f8429ee1b1 (raw.jsonl)
```

The full packet's `REVIEW.md` and `outside-repo.json` and the diagnostics packet's are
sealed by their `artifact-sha256.json`, so they keep their original wording
(`~/safeact-evidence`, "planned GitHub Release asset") on purpose. This note is the
pointer from those sealed entries to the published files.
