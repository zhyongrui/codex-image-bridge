# Repository Instructions

This repository is designed for Codex-assisted installation of the bundled
`codex-image-bridge` skill.

When a user asks to diagnose, install, repair, or uninstall the bridge:

1. Read `skills/codex-image-bridge/SKILL.md` and follow it exactly.
2. Run `preflight` before any mutation.
3. Use only the bundled manager for config and service changes; do not hand-edit
   `~/.codex/config.toml` or a LaunchAgent.
4. Never print or copy tokens, API keys, authorization headers, request bodies,
   or the complete Codex config.
5. Do not perform a billable image-generation smoke test unless explicitly
   requested.
6. Do not retry non-idempotent Responses or image POST requests.
7. Preserve rollback behavior and unrelated user configuration.

The canonical runtime files live in
`skills/codex-image-bridge/scripts/`. Run the test suite after changes:

```bash
python3 -m unittest -v tests/test_bridge.py tests/test_manager.py
```

Do not commit generated images, logs, local state, config backups, archives,
credentials, or personalized service definitions.
