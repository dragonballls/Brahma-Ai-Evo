"""Real Windows Phone Link surface plus authorized Brahma Connect phone transport."""
from core.selected_capabilities import execute_selected_capability

FEATURE_METADATA = {
    "name": "phone_link_bridge",
    "aliases": ["phone_link", "phone_link_bridge", "link_to_windows"],
    "description": "Open the installed Microsoft Phone Link app and route supported phone actions through an explicitly paired Brahma device.",
    "triggers": ["phone link", "open phone link", "link to windows", "control my phone"],
    "parameters": {"type": "OBJECT", "properties": {
        "action": {"type": "STRING", "description": "status, open, or route"},
        "target": {"type": "STRING", "description": "Paired phone or tablet name/id"},
        "remote_action": {"type": "STRING", "description": "Authorized device action"},
        "parameters": {"type": "OBJECT", "description": "Action parameters"},
    }}
}

def execute(**kwargs):
    return execute_selected_capability("phone_link_bridge", kwargs.pop("action", "status"), **kwargs)
