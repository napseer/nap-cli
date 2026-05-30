"""Shared constants for Napseer MCP server."""

# Discovery
DISCOVERY_VIEWS = {"titles", "paths", "summary", "metadata"}
DISCOVERY_DEFAULT_LIMIT = 25
DISCOVERY_COMPACT_MAX_LIMIT = 10_000
DISCOVERY_FULL_MAX_LIMIT = 10_000
DISCOVERY_SOURCE_PAGE_LIMIT = 10_000
DISCOVERY_CURSOR_GUARD_PAGES = 100

# Lifecycle statuses
ACTIVE_EXCLUDED_STATUSES = {
    "completed",
    "complete_verified_uncommitted",
    "complete_pending_human_e2e",
    "superseded",
    "cancelled",
    "archived",
    "done",
}
KNOWN_LIFECYCLE_STATUSES = {
    "active",
    "archived",
    "backlog",
    "blocked",
    "cancelled",
    "completed",
    "complete_pending_human_e2e",
    "complete_verified_uncommitted",
    "current",
    "doing",
    "done",
    "draft",
    "in_progress",
    "pending",
    "planned",
    "review",
    "reviewed_plan",
    "superseded",
    "todo",
}

# Summary metadata keys by node type
SUMMARY_METADATA_KEYS = {
    "/plans": ["status", "priority", "scope", "owner", "agent_owner", "implementation_allowed", "blocked_by"],
    "/kanban": ["status", "column", "priority", "owner", "assignee", "blocked", "blocked_by", "lifecycle_state", "rank", "order", "due_date"],
    "/decisions": ["status", "date", "decision_category", "decision_area", "supersedes", "superseded_by"],
    "/implementation-notes": ["status", "date", "repos", "verification_summary"],
    "/reviews": ["status", "date", "severity_counts"],
    "/rules": ["status", "scope", "source"],
    "/tasks": ["status", "priority", "blocked_by", "owner", "assignee"],
}

# Kanban
KANBAN_DONE_STATUSES = {"done", "cancelled", "archived"}
DEFAULT_KANBAN_COLUMNS = ["backlog", "todo", "doing", "review", "done"]
DEFAULT_KANBAN_PRIORITIES = ["low", "normal", "high", "urgent"]
KANBAN_RELATIONS = {
    "blocked_by",
    "blocks",
    "follows",
    "precedes",
    "relates_to",
    "duplicates",
    "duplicate_of",
    "parent_of",
    "child_of",
    "tracks",
    "tracked_by",
    "milestone_of",
    "has_milestone",
    "mutually_exclusive_with",
    "implements",
    "references",
}
KANBAN_RELATION_BLOCKED_BY = "blocked_by"
KANBAN_RELATION_BLOCKS = "blocks"
KANBAN_RELATIONS_SOFT_DEPENDENCY = {"follows", "precedes"}
KANBAN_RELATIONS_RELATED = {"relates_to", "tracks", "tracked_by"}
KANBAN_RELATION_CHILD_OF = "child_of"
KANBAN_RELATION_PARENT_OF = "parent_of"
KANBAN_RELATIONS_DUPLICATE = {"duplicates", "duplicate_of"}
KANBAN_RELATIONS_MILESTONE = {"milestone_of", "has_milestone"}
KANBAN_RELATION_MUTUALLY_EXCLUSIVE = "mutually_exclusive_with"
KANBAN_DEPENDENCY_VIEWS = ["ready", "blocked", "blocking", "related", "children", "duplicates"]
KANBAN_DEPENDENCY_VIEW_READY = "ready"
KANBAN_DEPENDENCY_VIEW_BLOCKED = "blocked"
KANBAN_DEPENDENCY_VIEW_BLOCKING = "blocking"
KANBAN_DEPENDENCY_VIEW_RELATED = "related"
KANBAN_DEPENDENCY_VIEW_CHILDREN = "children"
KANBAN_DEPENDENCY_VIEW_DUPLICATES = "duplicates"
KANBAN_EXTERNAL_BLOCKERS_KEY = "external_blockers"
KANBAN_BLOCK_STATE_OPEN_HARD_KEY = "open_hard_blockers"
KANBAN_RESOLVED_EXTERNAL_STATES = {"done", "closed", "resolved"}
KANBAN_SOFT_BLOCKER_STRENGTH = "soft"
KANBAN_PRIORITY_RANK = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
KANBAN_COLUMN_LIFECYCLE = {
    "backlog": "open",
    "todo": "open",
    "doing": "active",
    "review": "review",
    "done": "done",
}

# Planning
PLANNING_SOURCE_RELATIONS = {
    "created_from",
    "derived_from",
    "implements",
    "references",
    "tracks",
    "verifies",
}
PLAN_ORIGIN_RELATIONS = {
    "created_from",
    "derived_from",
    "implements",
    "references",
    "tracks",
}


def discovery_max_limit(view):
    """Get the max limit for a discovery view."""
    return DISCOVERY_FULL_MAX_LIMIT if view == "full" else DISCOVERY_COMPACT_MAX_LIMIT
