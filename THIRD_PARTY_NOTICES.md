# Third-party notices

This source snapshot derives from https://github.com/dctongsheng/game-harness-lab/tree/hhr-yuanshen (base commit `1a7b0ca`). Original source notices remain intact. The Genshin runtime, desktop UI and derived integration are GPL-3.0-or-later; unrelated upstream components retain their own licenses.

- BetterGI: https://github.com/babalae/better-genshin-impact, GPL-3.0, pinned base `b3f90e70494320639eeccc8c559612d02d2d9ef0`. `integrations/bettergi/source.json` identifies the base and checksum of the complete development patch. `scripts/setup_genshin_bridge.py` reconstructs the modified source, preserving upstream license and branding. No BetterGI binary is included in this source-only release.
- AutoCommission: https://github.com/LX666-666/AutoCommission, GPL-3.0, pinned `f9b5a49942b613c545e9bea3bed4d015424844d1`. Exact source, recognition templates, author metadata and LICENSE remain in `integrations/auto_commission/upstream/`; hashes are recorded in `integrations/auto_commission/source.json`. The separate task adapter is GPL-3.0-or-later. Game artwork and marks retain their respective owners' rights.
- Google Harness²: https://github.com/google-research/harness2, Apache-2.0, pinned `1cfe254e8d25b15f37048c3730134686abe60335`. Optional optimizer dependency; see `platform/LICENSE.Harness2`. It is not bundled into the Windows agent.
- Vosk API and the Chinese ASR model: Apache-2.0; the model is downloaded from the official source at build time with a pinned hash. See `desktop/THIRD_PARTY_NOTICES.txt` and `desktop/APACHE-2.0.txt`.
- Python, .NET and other dependencies retain their distribution licenses. The desktop build copies dependency notices and corresponding modified source into the distributable payload.

This repository does not include the proprietary game client, private runtime evidence, account settings, credentials, or legacy installer binaries.
