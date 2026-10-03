from __future__ import annotations

import argparse

from features.autonomous_mission.skill import run_worker


def main() -> int:
    parser = argparse.ArgumentParser(description="Brahma Evo autonomous mission worker")
    parser.add_argument("--mission-id", required=True)
    args = parser.parse_args()
    return int(run_worker(args.mission_id))


if __name__ == "__main__":
    raise SystemExit(main())
