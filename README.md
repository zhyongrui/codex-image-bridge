# Codex Image Bridge

Let Codex diagnose and repair image generation for third-party Responses API
providers that do not implement the standalone Images API required by newer
Codex versions.

## Let Codex Fix It

Send this prompt to Codex:

```text
Review and use https://github.com/zhyongrui/codex-image-bridge to diagnose and
repair image generation for my active third-party Codex provider. Run the
read-only preflight first, never expose my API key, and use the bundled manager
instead of editing my Codex config manually.
```

Codex will inspect the repository, run a read-only plan, install the local
bridge when applicable, validate the service, and tell you when to open a new
task and test `$imagegen`.

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
- `~/Library/LaunchAgents/com.codex.image-bridge.plist`: runs the local service

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

Automatic service installation currently supports macOS. The proxy itself uses
only the Python standard library.

## Development

```bash
python3 -m unittest -v tests/test_bridge.py tests/test_manager.py
```

See [README.zh-CN.md](README.zh-CN.md) for Chinese instructions.
