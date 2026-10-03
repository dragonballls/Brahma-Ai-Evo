"""Low-overhead spatial stereo audio feature."""
from core.selected_capabilities import execute_selected_capability

FEATURE_METADATA = {
    "name": "spatial_audio",
    "aliases": ["spatial_audio", "3d_audio"],
    "description": "Apply equal-power left/right spatialization or play a short diagnostic tone without a persistent audio loop.",
    "triggers": ["spatial audio", "3d audio", "position audio", "place the sound"],
    "parameters": {"type": "OBJECT", "properties": {
        "action": {"type": "STRING", "description": "gains, samples, or play"},
        "azimuth_deg": {"type": "NUMBER", "description": "Sound direction in degrees, -180 to 180"},
        "distance": {"type": "NUMBER", "description": "Relative sound distance"},
        "samples": {"type": "ARRAY", "description": "Existing mono samples to spatialize"},
        "frequency_hz": {"type": "NUMBER", "description": "Diagnostic tone frequency"},
        "duration_s": {"type": "NUMBER", "description": "Diagnostic tone duration"},
    }}
}

def execute(**kwargs):
    return execute_selected_capability("spatial_audio", kwargs.pop("action", "gains"), **kwargs)
