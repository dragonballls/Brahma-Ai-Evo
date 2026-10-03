from __future__ import annotations

import json

from features.autonomous_mission.skill import recover_active_missions


def run_once() -> dict:
    return recover_active_missions()


def main() -> int:
    result = run_once()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
