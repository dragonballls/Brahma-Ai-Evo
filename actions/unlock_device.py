# actions/unlock_device.py

import json
from pathlib import Path
from actions.brahma_connect import connect_execute

def _get_settings() -> dict:
    """Read device settings through the canonical persistent settings layer."""
    from memory.config_manager import load_settings

    settings = load_settings()
    if not isinstance(settings, dict):
        raise RuntimeError("Device settings have an invalid root schema.")
    return settings

def unlock_device(parameters: dict, response=None, player=None, session_memory=None) -> str:
    target = parameters.get("target") or parameters.get("device_id")
    if not target:
        return "Error: You must specify a target device to unlock."

    settings = _get_settings()
    device_pins = settings.get("device_pins", {})
    if not isinstance(device_pins, dict):
        return "Error: Device PIN settings are malformed; refusing to use or replace them."

    # Try to find pin by exact target match first
    pin = device_pins.get(target)
    
    # If not found, try to resolve the device to get its actual ID from connect gateway
    if not pin:
        try:
            from actions.brahma_connect import connect_get_device
            res_str = connect_get_device({"target": target})
            res_dict = json.loads(res_str)
            if res_dict.get("success"):
                device_id = res_dict.get("device", {}).get("device_id")
                if device_id:
                    pin = device_pins.get(device_id)
        except Exception:
            pass
            
    if not pin:
        return f"Error: No unlock PIN is saved for device '{target}'. Please set the PIN in the UI first."
        
    # Send unlock command via brahma connect
    command_params = {
        "target": target,
        "action": "unlock_phone",
        "parameters": {
            "pin": pin
        }
    }
    
    if player:
        player.write_log(f"Sending unlock command to {target}...")
        
    result_str = connect_execute(command_params)
    
    try:
        result_dict = json.loads(result_str)
        if result_dict.get("success"):
            return f"Successfully sent unlock command to {target}."
        else:
            err = result_dict.get("error", "Unknown error")
            return f"Failed to unlock {target}: {err}"
    except Exception as e:
        return f"Error executing unlock command: {result_str}"
