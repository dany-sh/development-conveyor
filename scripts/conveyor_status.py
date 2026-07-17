#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from development_conveyor.cli import execute

parser = argparse.ArgumentParser()
parser.add_argument("--project")
args = parser.parse_args()
arguments = ["status"] + (["--project", args.project] if args.project else [])
print(json.dumps(execute(arguments, root=ROOT), indent=2, sort_keys=True))

