#!/usr/bin/env python3
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from development_conveyor.cli import execute

print(json.dumps(execute(["validate-config"], root=ROOT), indent=2, sort_keys=True))

