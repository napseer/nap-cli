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
- `nap auth login` stores the shared account session in the user data folder
  (`~/.local/share/napseer/credentials/default.json` on Linux; platform user data directories
  on macOS/Windows). `NAPSEER_USER_DATA_DIR` overrides that location.
  `nap auth login --project` selects a local override. Legacy repository
  credentials remain an explicit override until `nap auth migrate` moves them.
  Gateway identities remain separate in `.napseer/gateway-auth.json`.
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

MCP admits at most 32 unfinished requests by default, including cancelled work
that is still cleaning up. `-32097` means a request was rejected before
execution; retry after capacity is available. `-32098` means the result
is uncertain: inspect current state before retrying a mutation. Cancellation
prevents queued mutations from starting and attempts to release acquired locks.
It cannot undo a mutation already accepted by the API.

The supervisor waits up to 60 seconds for a response, then requests cancellation.
After 10 seconds without a cleanup acknowledgement, it terminates the worker and
fails affected requests without replay. Worker input writes have a five-second
deadline. Override these limits with `NAPSEER_MCP_RESPONSE_TIMEOUT_SECONDS`,
`NAPSEER_MCP_CANCEL_GRACE_SECONDS`, `NAPSEER_MCP_WRITE_TIMEOUT_SECONDS`, and
`NAPSEER_MCP_MAX_IN_FLIGHT_REQUESTS`. New source loads only after unfinished work
has drained. `nap_contract` exposes the runtime's request lifecycle contract.
These process recovery guarantees require the supervisor; the direct worker is
for isolated protocol debugging. An already-running older supervisor needs a
client reconnect after updating.

The public operator surface is intentionally small:

```text
nap init | status | doctor | auth | project | files | export | mcp | gateway | update | version | help
```

Authentication has one recovery verb: `nap auth repair`. Normal refresh is
automatic. `nap gateway repair` creates the separate worker identity once;
`--replace` is required to replace an existing identity.

Agents start only after the user configures an account login or a delegated API
key. With an account login, `nap init` creates a project owned by that account.
A committed `.napseer/project.json` always selects the existing project; it
never causes a replacement to be created. Concurrent refreshes share a bounded
credential lock, while project overrides remain independent. Network failures
preserve credentials and offer recovery through `nap auth repair`.

`nap auth keys --help` describes scoped API key issuance;
`nap auth api-key --env VARIABLE_NAME` installs a delegated key privately.
Keys can expire or be revoked and cannot approve agent decisions. Use separate
keys when agents need separate identities or permissions.

Upgrading from 0.2.x to 0.3.0 requires the current bootstrap once because old
installers have a fixed list of runtime files. `nap update` on those versions
rejects the new bundle before changing the active installation. Run:

```sh
curl -fsSL https://api.napseer.com/install | python3 -
```

The bootstrap verifies every asset and switches the runtime atomically without
changing credentials or project locators. Restart existing MCP client sessions
after this upgrade; subsequent releases use `nap update` normally.

In Bash, run commands as `nap update`, `nap auth login`, and so on. A leading
`!` is shell history expansion: `!nap update` replays the most recent command
whose text starts with `nap` and appends `update`; it does not invoke an
alternate Napseer update mode.

The direct wrapper remains the `nap` CLI implementation and an isolated
protocol-debugging entrypoint.
