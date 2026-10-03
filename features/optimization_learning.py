"""Adaptive low-power optimization learner."""
from core.selected_capabilities import execute_selected_capability

FEATURE_METADATA = {
    "name": "optimization_learning",
    "aliases": ["optimization_learning", "performance_learning"],
    "description": "Learn resource-load patterns and produce conservative optimization recommendations without changing system settings automatically.",
    "triggers": ["optimize learning", "optimization learning", "learn my performance profile", "reduce resource use"],
    "parameters": {"type": "OBJECT", "properties": {
        "action": {"type": "STRING", "description": "observe, recommendations, or status"},
        "cpu_percent": {"type": "NUMBER", "description": "Current CPU utilization"},
        "memory_percent": {"type": "NUMBER", "description": "Current memory utilization"},
        "battery_percent": {"type": "NUMBER", "description": "Optional battery level"},
        "gpu_percent": {"type": "NUMBER", "description": "Optional GPU utilization"},
    }}
}

def execute(**kwargs):
    return execute_selected_capability("optimization_learning", kwargs.pop("action", "observe"), **kwargs)
