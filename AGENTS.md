# Repository Instructions

This repository is designed for Codex-assisted installation of the bundled
`codex-image-bridge` skill.

When a user asks to diagnose, install, repair, or uninstall the bridge:

1. If the user supplied only the repository URL, inspect or check out the
   repository first; never execute an opaque remote script.
2. Read `skills/codex-image-bridge/SKILL.md` and follow it exactly.
3. Run `preflight` before any mutation.
4. Use only the bundled manager for config and service changes; do not hand-edit
   `~/.codex/config.toml` or a LaunchAgent.
5. If macOS `launchctl bootstrap` or Windows Task Scheduler registration is
   denied by the Codex sandbox, request scoped elevated execution and rerun the
   same installer. Do not use `sudo` or an Administrator shell by default, and
   do not hand the task back to a separate terminal until escalation is
   unavailable or denied.
6. Never print or copy tokens, API keys, authorization headers, request bodies,
   or the complete Codex config.
7. Do not perform a billable image-generation smoke test unless explicitly
   requested.
8. Do not retry non-idempotent Responses or image POST requests.
9. Preserve rollback behavior and unrelated user configuration.

The canonical runtime files live in
`skills/codex-image-bridge/scripts/`. Run the test suite after changes:

```bash
python3 -m unittest -v tests/test_bridge.py tests/test_manager.py
```

Do not commit generated images, logs, local state, config backups, archives,
credentials, or personalized service definitions.

Windows support is developed on the `windows-support` branch until the native
validation checklist in the corresponding GitHub issue passes. Do not merge it
based only on the cross-platform unit-test matrix.
