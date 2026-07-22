---
name: codex-image-bridge
description: Diagnose, install, verify, upgrade, or uninstall a local compatibility bridge when Codex image generation fails with a third-party Responses API provider that lacks /images/generations or /images/edits. Use for imagegen network errors, 404/405 image endpoint failures, post-update image-generation regressions, bridge health checks, and safe restoration of the original provider URL.
---

# Codex Image Bridge

Use the bundled manager instead of manually editing Codex configuration. The
manager preserves credentials, creates backups, validates TOML, and rolls back
failed installations.

## Diagnose

1. Set `skill_dir` to the directory containing this `SKILL.md`.
2. Run the read-only preflight:

```bash
/usr/bin/python3 "$skill_dir/scripts/bridge_manager.py" preflight
```

3. Read the JSON. Do not install automatically when `applicable` is false.
4. Explain that preflight does not prove upstream `image_generation` support.
   Use the user's existing error evidence. Do not send a billable image request
   unless the user explicitly asks for a live generation test.

For an existing installation, run:

```bash
"$HOME/.codex/image-bridge/bridge_manager.py" doctor
```

Diagnosis alone does not authorize installation or configuration changes.

## Install Or Repair

When the user asks to fix, install, or repair the problem:

1. Run preflight first and summarize the three paths in `planned_changes`.
2. Do not print `config.toml`, authentication headers, tokens, or request bodies.
3. Run the deterministic installer:

```bash
/usr/bin/python3 "$skill_dir/scripts/bridge_manager.py" install
```

4. If the sandbox denies `launchctl bootstrap`, immediately request scoped
   elevated execution and rerun the same installer command outside the sandbox.
   Explain that elevation is required only to register the current user's
   LaunchAgent. Do not use `sudo`. Do not send the user to Terminal before
   attempting the product's approval/escalation mechanism. A normal rollback
   after the first denied attempt is expected and makes the retry safe.
5. Run the installed doctor:

```bash
"$HOME/.codex/image-bridge/bridge_manager.py" doctor
```

6. Report the bridge version, runtime, config, TLS, and service results.
7. Ask the user to restart Codex or open a new task, then invoke `$imagegen`.
   Only perform a real image generation in the current task when explicitly
   requested because it can consume quota.

Never add retries for Responses or image POST requests. A failed connection
does not prove the upstream did not receive the request.

## Uninstall

When the user explicitly asks to remove or restore the bridge, run:

```bash
"$HOME/.codex/image-bridge/bridge_manager.py" uninstall
```

The manager restores the original URL only when the current URL still matches
the installed bridge URL. Report when it intentionally leaves a newer user
configuration unchanged.

## Boundaries

- Automatic background-service installation currently supports macOS only.
- If scoped elevation is unavailable or the user declines it, stop after the
  automatic rollback and provide the exact non-`sudo` Terminal command as the
  final fallback. Do not describe sandbox denial as a bridge failure.
- Never replace the current config wholesale with a backup.
- Never persist credentials outside the user's existing Codex configuration.
- Never expose the bridge beyond a loopback address.
- Read `references/troubleshooting.md` only when preflight, installation, or
  doctor reports a failure.
