#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from development_conveyor.config import load_configuration
from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.recovery import assess_recovery
from development_conveyor.registry import ProjectRegistry
from development_conveyor.repository import RepositoryInspector

parser = argparse.ArgumentParser()
parser.add_argument("--project", required=True)
args = parser.parse_args()
configuration = load_configuration(ROOT)
project = ProjectRegistry(configuration).get(args.project)
engine = CycleEngine(configuration)
inspector = RepositoryInspector(project.repository)
state = engine.cycle_store.read(inspector.cycle_state_path())
assessment = assess_recovery(project, state)
print(json.dumps({
    "outcome": assessment.outcome,
    "resume_phase": assessment.resume_phase,
    "evidence": assessment.evidence,
    "human_decision": assessment.human_decision,
}, indent=2, sort_keys=True))

