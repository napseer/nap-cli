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
- `.napseer/project.json` is the commit-safe project locator. It contains only
  the schema, API origin, project UUID, and slug; it never contains tokens,
  account or worker identity, encryption state, keys, passphrases, or claim
  links. A fresh clone uses it to attach the intended project.
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

If `NAPSEER_MCP_WORKER_PATH` points to a stable launcher that loads a separate,
replaceable runtime, set `NAPSEER_MCP_WORKER_WATCH_PATHS` to the
`os.pathsep`-separated runtime paths. The supervisor then reloads the launcher
after any watched runtime is atomically replaced, preserving the same bounded,
non-replaying request lifecycle used for direct workers.

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

A cloned repository that already commits `.napseer/project.json` is not a new
workspace: `nap init` and project-scoped MCP tools fail closed instead of
creating a duplicate anonymous project. Run `nap project attach`; the OAuth
approval is constrained to the locator's project after normal server-side
access validation. If that project is still anonymous, claim it from a machine
that retains its enrollment identity, then attach from the new computer.

In Bash, run commands as `nap update`, `nap auth login`, and so on. A leading
`!` is shell history expansion: `!nap update` replays the most recent command
whose text starts with `nap` and appends `update`; it does not invoke an
alternate Napseer update mode.

The direct wrapper remains the `nap` CLI implementation and an isolated
protocol-debugging entrypoint.
