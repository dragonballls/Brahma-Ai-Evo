"""Opt-in Life360 family location bridge through an authorized Home Assistant feed."""
from core.selected_capabilities import execute_selected_capability

FEATURE_METADATA = {
    "name": "life360_family",
    "aliases": ["life360", "family_tracking", "family_locations"],
    "description": "Read authorized Life360 family device_tracker locations exposed by a local/private Home Assistant instance and feed them into God’s Eye.",
    "triggers": ["life360", "family tracking", "family location", "where is my family"],
    "parameters": {"type": "OBJECT", "properties": {
        "action": {"type": "STRING", "description": "status or locations"},
    }}
}

def execute(**kwargs):
    return execute_selected_capability("life360_family", kwargs.pop("action", "status"), **kwargs)
