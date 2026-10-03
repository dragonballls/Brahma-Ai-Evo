"""Expanded God’s Eye feed using current Brahma Connect and family providers."""
from core.selected_capabilities import execute_selected_capability

FEATURE_METADATA = {
    "name": "gods_eye_expansion",
    "aliases": ["gods_eye_expansion", "gods_eye_global"],
    "description": "Expose the current God’s Eye globe payload together with authorized connected-device and family location feeds.",
    "triggers": ["expand gods eye", "gods eye expansion", "show connected locations", "show family locations"],
    "parameters": {"type": "OBJECT", "properties": {
        "action": {"type": "STRING", "description": "snapshot or globe"},
    }}
}

def execute(**kwargs):
    return execute_selected_capability("gods_eye_expansion", kwargs.pop("action", "snapshot"), **kwargs)
