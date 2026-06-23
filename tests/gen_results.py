#!/usr/bin/env python3
"""Scan results/ and write results.json manifest for viewer.html."""
import json
from pathlib import Path

results_dir = Path(__file__).parent / "results"
files = sorted(p.name for p in results_dir.glob("*.js"))
manifest = Path(__file__).parent / "results.json"
manifest.write_text(json.dumps(files, indent=2) + "\n")
print(f"results.json: {len(files)} files")
