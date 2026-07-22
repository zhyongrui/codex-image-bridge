# Codex Image Bridge

Let Codex diagnose and repair image generation for third-party Responses API
providers that do not implement the standalone Images API required by newer
Codex versions.

## Let Codex Fix It

Send this prompt to Codex:

```text
Use https://github.com/zhyongrui/codex-image-bridge to fix image generation for
my current Codex provider. Read and follow the repository instructions.
```

All safety, preflight, installation, validation, rollback, and credential rules
live in `AGENTS.md` and the bundled skill. Codex will read them from the
repository instead of requiring a long user prompt.

## Install As A Reusable Skill

You can also ask Codex:

```text
Install the codex-image-bridge skill from
https://github.com/zhyongrui/codex-image-bridge/tree/main/skills/codex-image-bridge
and tell me when to open a new task.
```

Then invoke:

```text
$codex-image-bridge repair image generation for my active provider
```

## What It Changes

- `~/.codex/config.toml`: changes only the active provider `base_url`
- `~/.codex/image-bridge/`: installs credential-free runtime files and state
- macOS: `~/Library/LaunchAgents/com.codex.image-bridge.plist` runs the service
- Windows: a per-user `Codex Image Bridge` Scheduled Task runs the service

The bridge listens only on loopback and forwards Codex's existing authorization
headers in memory. It never stores credentials in its source, state, or service
definition. Failed installs restore the prior config and service.

## Manual Commands

```bash
/usr/bin/python3 skills/codex-image-bridge/scripts/bridge_manager.py preflight
/usr/bin/python3 skills/codex-image-bridge/scripts/bridge_manager.py install
~/.codex/image-bridge/bridge_manager.py doctor
~/.codex/image-bridge/bridge_manager.py uninstall
```

macOS installation has native validation. Windows installation is under native
validation on the `windows-support` branch; a passing unit-test matrix alone is
not a Windows release sign-off. The proxy uses only the Python standard library.

## Development

```bash
python3 -m unittest -v tests/test_bridge.py tests/test_manager.py
```

See [README.zh-CN.md](README.zh-CN.md) for Chinese instructions.
