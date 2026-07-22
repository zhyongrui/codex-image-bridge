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
