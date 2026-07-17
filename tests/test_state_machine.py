import unittest

from development_conveyor.errors import InvalidTransition
from development_conveyor.state_machine import (
    CYCLE_MACHINE, CYCLE_STATES, CYCLE_TRANSITIONS,
    PORTFOLIO_MACHINE, PORTFOLIO_STATES, PORTFOLIO_TRANSITIONS,
)


class StateMachineTests(unittest.TestCase):
    def test_every_declared_edge_and_idempotent_transition(self):
        for machine, states, transitions in (
            (PORTFOLIO_MACHINE, PORTFOLIO_STATES, PORTFOLIO_TRANSITIONS),
            (CYCLE_MACHINE, CYCLE_STATES, CYCLE_TRANSITIONS),
        ):
            self.assertEqual(set(states), set(transitions))
            for state in states:
                self.assertFalse(machine.transition(state, state).changed)
                for target in transitions[state]:
                    self.assertTrue(machine.transition(state, target).changed)

    def test_invalid_transition_is_rejected(self):
        with self.assertRaises(InvalidTransition):
            CYCLE_MACHINE.transition("idle", "integrated" if "integrated" in CYCLE_STATES else "completed")
        with self.assertRaises(InvalidTransition):
            PORTFOLIO_MACHINE.transition("disabled", "feature_running")

