"""Regenerate fixtures/missions.json (synthetic, deterministic).

python scripts/make_fixtures.py [--seed 1] [--n 10]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from aiceo.scenarios import generate_missions

OUT = Path(__file__).resolve().parents[1] / "fixtures" / "missions.json"


def render(seed: int = 1, n: int = 10) -> str:
    return json.dumps(generate_missions(seed, n), indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--n", type=int, default=10)
    args = parser.parse_args()
    OUT.write_text(render(args.seed, args.n), encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
