"""Advanced deterministic analysis without model or network overhead."""
from core.selected_capabilities import execute_selected_capability

FEATURE_METADATA = {
    "name": "advanced_analysis",
    "aliases": ["advanced_analysis", "analyze_data"],
    "description": "Analyze numeric samples or event records with trend, percentile, variance, IQR outliers, and change metrics.",
    "triggers": ["advanced analysis", "analyze these numbers", "find outliers", "analyze trend"],
    "parameters": {"type": "OBJECT", "properties": {
        "values": {"type": "ARRAY", "description": "Numeric samples"},
        "events": {"type": "ARRAY", "description": "Event objects containing a numeric field"},
        "key": {"type": "STRING", "description": "Numeric event field name"},
        "action": {"type": "STRING", "description": "values, events, or status"},
    }}
}

def execute(**kwargs):
    return execute_selected_capability("advanced_analysis", kwargs.pop("action", "values"), **kwargs)
