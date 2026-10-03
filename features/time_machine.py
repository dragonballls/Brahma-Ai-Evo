"""Persistent Brahma Time Machine snapshots and deterministic diffs."""
from core.selected_capabilities import execute_selected_capability

FEATURE_METADATA = {
    "name": "time_machine",
    "aliases": ["time_machine", "workspace_time_machine"],
    "description": "Save durable JSON state snapshots, compare them, list history, and return an explicit restore payload.",
    "triggers": ["time machine", "save snapshot", "compare snapshots", "restore snapshot"],
    "parameters": {"type": "OBJECT", "properties": {
        "action": {"type": "STRING", "description": "save, list, diff, or restore"},
        "name": {"type": "STRING", "description": "Snapshot name"},
        "state": {"type": "OBJECT", "description": "JSON-compatible state"},
        "tags": {"type": "ARRAY", "description": "Optional snapshot tags"},
        "first": {"type": "STRING", "description": "First snapshot id or name"},
        "second": {"type": "STRING", "description": "Second snapshot id or name"},
        "selector": {"type": "STRING", "description": "Snapshot id, name, or filename"},
        "limit": {"type": "INTEGER", "description": "Maximum history records"},
    }}
}

def execute(**kwargs):
    return execute_selected_capability("time_machine", kwargs.pop("action", "list"), **kwargs)
