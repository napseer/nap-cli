# Napseer CLI

Source import for the `nap` operator CLI.

This repository is intended to become the canonical source for:

- `nap` installer and update UX.
- Project bootstrap and status commands.
- Local authenticated MCP wrapper launch/update behavior.
- Gateway command facade commands such as `nap gateway start`, `nap gateway logs`, and `nap gateway update`.

Current state:

- `resources/scripts/nap_install.py` owns the versioned, verified, atomic
  runtime bundle installer.
- `resources/scripts/` contains the public runtime source embedded by the
  backend without behavioral rewrites.
- `resources/scripts/napseer_mcp_supervisor.py` is the recommended Codex stdio
  entrypoint. It keeps the client transport alive while restarting or reloading
  the generated `napseer_mcp_server.py` worker between requests.
- `scripts/nap_install.py` is kept as a convenient top-level command source copy during the initial import.
- Gateway service compatibility remains in the worker while the standalone
  gateway image is built from the public gateway repository.

Backend discovery publishes the release manifest and exact public source
revision used for each bundle.

Configure Codex and other long-lived stdio clients to launch:

```sh
python3 ~/.local/share/napseer/napseer_mcp_supervisor.py
```

The direct wrapper remains the `nap` CLI implementation and an isolated
protocol-debugging entrypoint.
