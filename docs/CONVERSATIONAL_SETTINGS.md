# Conversational JARVIS Settings

Brahma Evo now exposes settings through the settings_control JARVIS tool so users can describe the desired outcome instead of navigating the Settings UI.

Examples:
- "Turn off the startup animation."
- "Start Brahma minimized."
- "Use OpenRouter by default."
- "Make Brahma lighter on my PC."
- "Turn off those attention prompts."
- "Set the interface sounds to 40%."
- "Enable push to talk."
- "Use the efficiency performance profile."
- "Make the interface blue."

JARVIS maps the request to an allowlisted settings registry, validates the value, saves it to the persistent Brahma settings file, and reports the old/new value when available.

The interpreter supports both deterministic natural-language aliases and a constrained JSON mapping step for requests that need more language understanding. The JSON mapping step only sees the settings allowlist and a redacted version of the request; it does not receive arbitrary config-file write access.

Some startup/backend settings naturally take effect after restarting Brahma. Settings that the running process reads dynamically take effect on subsequent operations.

Secrets and credentials are deliberately kept outside the generic settings interpreter. Authentication-required integrations should continue to use their existing dedicated authentication flows rather than sending secrets through the generic settings mapper.

## Supported setting groups

The conversational registry includes startup behavior, AI provider selection, automatic provider fallback, attention prompts, developer mode, interface sound settings, push-to-talk, offline mode, intelligence mode/orchestration, local AI endpoint/model, desktop mode/performance profile, WorkerW backend, performance overlay, and theme.

The registry is intentionally allowlisted so a natural-language request cannot become an arbitrary filesystem or JSON mutation.
