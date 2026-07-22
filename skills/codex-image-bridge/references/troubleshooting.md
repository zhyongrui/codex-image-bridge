# Troubleshooting

## Preflight Is Not Applicable

- `platform` is not `darwin`: automatic service installation is unavailable.
- `runtime.found` is false: install Python 3.11+ or pass `--python` explicitly.
- `upstream` is null: if the config already points to a bridge and no state is
  available, recover the original provider URL from the user before installing.

## Doctor Failures

- `state`: rerun installation from the skill scripts.
- `runtime`: the cached Python runtime may have moved after a Codex update;
  rerun installation so runtime discovery generates a new service definition.
- `Codex config`: do not overwrite it manually. Compare the current provider
  with preflight and ask before reinstalling.
- `bridge health`: inspect `~/.codex/log/image-bridge.log` and
  `~/.codex/log/image-bridge.error.log` without printing request bodies or
  credentials, then rerun installation.
- `upstream TLS`: the provider or network is unavailable. A local reinstall
  cannot repair an upstream outage.
- `LaunchAgent`: rerun installation; it regenerates and reloads the service.

## Codex Sandbox Denies LaunchAgent Registration

Treat a denial at `launchctl bootstrap` as an execution-permission boundary,
not as an incompatible provider or broken bridge. The installer automatically
rolls back the config and service definition after the denied attempt.

1. Request scoped elevated execution with the justification: "Register the
   current user's local Codex Image Bridge LaunchAgent and complete the
   previously approved installation."
2. Rerun the exact same `bridge_manager.py install` command outside the sandbox.
3. Do not add `sudo`; this is a per-user LaunchAgent in the `gui/<uid>` domain.
4. Run doctor after the elevated installer succeeds.
5. Give the user a Terminal command only when the runtime has no approval or
   escalation capability, or when the user declines the request.

## HTTP Errors

- `404` or `405` from `/images/generations`: the provider likely lacks the
  standalone Images API and is a candidate for this bridge.
- `502` with TLS EOF: run doctor. Do not retry the POST automatically.
- `401` or `403`: preserve the upstream response and ask the user to verify
  provider credentials or image entitlement. Never display the credential.
- `429`: report quota or rate limiting; do not bypass it with retries.

## Logs

Use narrow reads such as:

```bash
tail -n 100 "$HOME/.codex/log/image-bridge.log"
tail -n 100 "$HOME/.codex/log/image-bridge.error.log"
```

The bridge intentionally omits request bodies and authorization headers.
