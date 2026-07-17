"""Source-checkout package shim for direct repository validation commands."""

from pathlib import Path

_SOURCE_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "development_conveyor"
__path__ = [str(_SOURCE_PACKAGE)]

from .state_machine import CYCLE_STATES, PORTFOLIO_STATES

__all__ = ["CYCLE_STATES", "PORTFOLIO_STATES"]
__version__ = "0.1.0"
