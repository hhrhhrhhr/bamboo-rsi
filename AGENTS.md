# Development workflow

This repository is the active Bamboo RSI development checkout. Make source edits,
run tests, and perform Git operations here. Treat the earlier parent checkout as
historical source and local runtime dependencies, rather than an editing target.

- Use `platform/.venv/Scripts/python.exe` for Windows Python work.
- Keep credentials, local settings, account identifiers, recordings, and raw run
  evidence in ignored local storage. A local `.runtime/genshin-home` junction may
  refer to existing evidence; preserve its records, budgets, and version lineage.
- Before changing running Agent code, native execution files, task resources,
  combat scripts, or configuration, stop the coordinator and verify service and
  input leases are released. Do not change frozen files during a live run.
- Game task selection and input must come from the project's Decision provider
  and BetterGI executor. Record developer intervention separately.
- Preserve failures and original evidence. Development rehearsals and same-day
  partial progress are not autonomous days. Keep the existing prospective
  14-game-day baseline/candidate validation and accepted-parent inheritance gate.
- Required route combat must finish with actual evidence; a timeout is failure.
- Live execution must record the game window, without microphone audio, before
  task execution and retain the playable video and run association afterwards.
- Run relevant regression checks and `python scripts/check_public_tree.py` before
  publication. Retain upstream attribution, pinned resources, and GPL notices.
