# Agent Instructions

This file is bootstrap guidance only. Napseer is the canonical project memory source.

## Operating Order

For non-trivial work:

1. Consult BotAI MCP for read-only advisory guidance.
2. Query Napseer for current project memory.
3. Inspect this repository or runtime before changing anything.
4. Make the smallest scoped change that satisfies the task.
5. Verify with focused proof.
6. Record durable outcomes in Napseer when behavior, product direction, security, plans, or operations change.

## Memory Source

Use Napseer as the only project memory source. Do not create scattered Markdown files for internal project understanding. Repository Markdown is only for bootstrap instructions or intentional public/versioned documentation.

Start with:

- `/rules/memory-source-policy`
- `/documentation/product/ideal-mcp-and-cli`
- `/documentation/features/gateway-terminal`
- `/indexes/project-memory-map`
- `/documentation/product/prd`

## Repository Scope

This repository owns the operator-facing `nap` CLI, local MCP wrapper installation/update UX, project/auth/status commands, memory ergonomics helpers, and gateway command facade. Backend discovery remains the compatibility source of truth; authenticated project memory writes go through the local MCP wrapper.

When CLI or MCP wrapper behavior changes, update the relevant Napseer node in the same work and verify with the focused CLI/MCP tests.
