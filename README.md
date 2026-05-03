# Napseer CLI

Source import for the `nap` operator CLI.

This repository is intended to become the canonical source for:

- `nap` installer and update UX.
- Project bootstrap and status commands.
- Local authenticated MCP wrapper launch/update behavior.
- Gateway command facade commands such as `nap gateway start`, `nap gateway logs`, and `nap gateway update`.

Current state:

- `resources/scripts/nap_install.py` is copied from the backend pre-split implementation.
- `resources/scripts/` contains the runtime scripts currently installed by `nap_install.py`.
- `scripts/nap_install.py` is kept as a convenient top-level command source copy during the initial import.
- Gateway-specific implementation is still partially embedded in `resources/scripts/napseer_mcp_server.py`; it should move behind a gateway package/runtime boundary in a later extraction slice.

Backend discovery remains the compatibility source of truth during migration.
