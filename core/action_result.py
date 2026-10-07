"""Small, dependency-free action-result failure classifier used across the application."""


def action_result_is_failure(result: object) -> bool:
    """Recognize explicit action failure without misclassifying valid zero-result messages."""
    if isinstance(result, dict):
        if result.get("success") is False or result.get("ok") is False:
            return True
        error = result.get("error")
        if error not in (None, ""):
            return True
        errors = result.get("errors")
        if errors:
            return True
        return False
    text = str(result or "").strip().casefold()
    if not text:
        return True
    return text.startswith((
        "error:", "failed", "failure:", "could not", "couldn't", "unable to",
        "cannot ", "can't ", "access denied:", "permission denied:", "not found:",
        "invalid ", "unsupported ", "timed out", "timeout:",
        "no running processes found", "could not detect display brightness",
        "no smart home devices are connected", "smart-home provider rejected",
        "smart home command failed", "failed to",
        "gmail credentials not configured", "invalid gmail", "recipient email address",
        "unknown google workspace", "unknown gmail action", "unknown calendar action",
        "unknown google workspace service",
    ))
