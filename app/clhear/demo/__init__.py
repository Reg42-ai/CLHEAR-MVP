"""Demo corpus for layers L2–L8 (illustrative, clearly labeled).

These seeds preview the shape of the derived layers before their HLDs ship.
They are authored, not derived — but every basis/controls ref points at a REAL
L1 clause (source_key + ref), so lineage inspection bottoms out in genuine
verbatim text, hashes and immutable originals. Restricted sources appear as
refs only, never text (HLD working rule 4).
"""
import json
from functools import lru_cache
from pathlib import Path

DEMO_DIR = Path(__file__).parent

FILES = {
    "L2": "l2_obligations.json",
    "L3": "l3_building_blocks.json",
    "L4": "l4_profiles.json",
    "L5": "l5_activities.json",
    "L6": "l6_programs.json",
    "L7": "l7_risk.json",
    "L8": "l8_benchmarks.json",
}


@lru_cache
def load_layer_items(layer: str) -> list[dict]:
    name = FILES.get(layer)
    if not name:
        return []
    return json.loads((DEMO_DIR / name).read_text())


def demo_counts() -> dict[str, int]:
    return {layer: len(load_layer_items(layer)) for layer in FILES}
