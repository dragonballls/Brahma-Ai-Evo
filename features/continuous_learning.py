"""Continuous learning feature backed by Brahma's persistent learned-rules engine."""
from core.selected_capabilities import execute_selected_capability

FEATURE_METADATA = {
    "name": "continuous_learning",
    "aliases": ["learning_engine", "continuous_learning"],
    "description": "Record useful interaction outcomes and explicit feedback so Brahma can improve future behavior without a local model.",
    "triggers": ["learn from this", "remember this correction", "learn my preference", "continuous learning", "learning engine"],
    "parameters": {"type": "OBJECT", "properties": {
        "action": {"type": "STRING", "description": "record, feedback, or status"},
        "signal": {"type": "STRING", "description": "Behavior or event to learn from"},
        "outcome": {"type": "STRING", "description": "Observed result"},
        "score": {"type": "NUMBER", "description": "Optional outcome score"},
        "context": {"type": "OBJECT", "description": "Small structured context"},
        "rule": {"type": "STRING", "description": "Explicit behavioral feedback to remember"},
        "category": {"type": "STRING", "description": "general, formatting, workflow, or habit"},
    }}
}

def execute(**kwargs):
    return execute_selected_capability("continuous_learning", kwargs.pop("action", "status"), **kwargs)
