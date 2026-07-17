#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from development_conveyor.cli import execute

parser = argparse.ArgumentParser()
parser.add_argument("--project", required=True)
args = parser.parse_args()
print(json.dumps(execute(["plan", "--project", args.project, "--dry-run"], root=ROOT), indent=2, sort_keys=True))

