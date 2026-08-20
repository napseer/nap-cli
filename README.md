# Napseer CLI

Source import for the `nap` operator CLI.

This repository is intended to become the canonical source for:

- `nap` installer and update UX.
- Project bootstrap and status commands.
- Local authenticated MCP wrapper launch/update behavior.
- Gateway lifecycle commands such as `nap gateway setup`, `repair`, `start`,
  `status`, `logs`, `terminal`, `schedule`, and `vault`.

Current state:

- `resources/scripts/nap_install.py` owns the versioned, verified, atomic
  runtime bundle installer.
- `resources/scripts/` contains the public runtime source embedded by the
  backend without behavioral rewrites.
- `resources/scripts/napseer_mcp_supervisor.py` is the recommended Codex stdio
  entrypoint. It keeps the client transport alive while restarting or reloading
  the generated `napseer_mcp_server.py` worker between requests.
- Gateway service compatibility remains in the worker while the standalone
  gateway image is built from the public gateway repository.
- `nap mcp status` and `nap doctor` run a fresh stdio initialization,
  `tools/list`, and authenticated read probe. They deliberately report an
  existing client connection as `not_observable`; a successful fresh probe
  does not claim that a previously opened Codex transport is connected.
- Operator OAuth credentials remain in `.napseer/auth.json`. A repaired local
  gateway uses a separate `.napseer/gateway-auth.json` worker identity, so
  relay token renewal cannot rotate or overwrite the operator/MCP session.
- CLI terminal and schedule operations call the running gateway's
  loopback-only, CSRF-protected control API. PTYs therefore live in the daemon
  instead of disappearing when a CLI subprocess exits.

Backend discovery publishes the release manifest and exact public source
revision used for each bundle.

Configure Codex and other long-lived stdio clients to launch:

```sh
python3 ~/.local/share/napseer/napseer_mcp_supervisor.py
```

For repositories that may be resumed after Codex opened a different working
directory, add a trusted project `.codex/config.toml` override with both
`cwd` and `NAPSEER_PROJECT_ROOT`. MCP processes inherit their launch directory;
they cannot infer a later client workspace switch over stdio.

The public operator surface is intentionally small:

```text
nap init | status | doctor | auth | project | mcp | gateway | update | version | help
```

Authentication has one recovery verb: `nap auth repair`. Normal refresh is
automatic. `nap gateway repair` creates the separate worker identity once;
`--replace` is required to replace an existing identity.

Project-scoped MCP tools are zero-login by default. In a new workspace, the
first such tool enrolls an anonymous worker and creates its first project
automatically; `nap init` remains the explicit equivalent. Anonymous access
and refresh-token or SSH-key recovery continue without operator
authentication. Authentication through `nap project claim` is required only
after the anonymous account reaches its service limit (currently one project
or 100 stored nodes), or when its durable local enrollment identity is no
longer recoverable.

In Bash, run commands as `nap update`, `nap auth login`, and so on. A leading
`!` is shell history expansion: `!nap update` replays the most recent command
whose text starts with `nap` and appends `update`; it does not invoke an
alternate Napseer update mode.

The direct wrapper remains the `nap` CLI implementation and an isolated
protocol-debugging entrypoint.
