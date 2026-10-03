"""Authorized remote-computing feature using Brahma Connect capabilities."""
from core.selected_capabilities import execute_selected_capability

FEATURE_METADATA = {
    "name": "remote_computing",
    "aliases": ["remote_computing", "remote_pc"],
    "description": "Discover paired Windows/PC computers and route capability-gated commands through Brahma Connect.",
    "triggers": ["remote computing", "remote computer", "control remote pc", "run on remote computer"],
    "parameters": {"type": "OBJECT", "properties": {
        "action": {"type": "STRING", "description": "list or route"},
        "target": {"type": "STRING", "description": "Paired computer name or id"},
        "remote_action": {"type": "STRING", "description": "Authorized device action"},
        "parameters": {"type": "OBJECT", "description": "Action parameters"},
    }}
}

def execute(**kwargs):
    return execute_selected_capability("remote_computing", kwargs.pop("action", "list"), **kwargs)
