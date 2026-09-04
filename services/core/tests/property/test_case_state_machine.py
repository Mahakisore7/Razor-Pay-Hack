"""Stateful property tests for Case's transition table (DOMAIN-MODEL SS4.2).

I1: a case reaches exactly one terminal state, exactly once -- once
terminal, it can never be moved again. I7: a control-arm case never reaches
EXECUTING. Hypothesis drives random sequences of transition attempts and
checks both invariants after every step, covering walks through the state
machine a handful of hand-written examples would not think to try.
"""

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from recoup.domain.case import TERMINAL_STATES, Arm, Case, CaseState, IllegalTransition
from tests.factories import make_case


class CaseStateMachine(RuleBasedStateMachine):
    case: Case
    arm: Arm
    terminal_reached: CaseState | None

    @initialize(arm=st.sampled_from(list(Arm)))
    def setup(self, arm: Arm) -> None:
        self.arm = arm
        self.case = make_case(state=CaseState.DETECTED, arm=arm)
        self.terminal_reached = None

    @rule(target_state=st.sampled_from(list(CaseState)))
    def attempt_transition(self, target_state: CaseState) -> None:
        before = self.case.state
        try:
            self.case.transition_to(target_state)
        except IllegalTransition:
            assert self.case.state == before  # a rejected attempt never mutates state
            return

        if target_state in TERMINAL_STATES:
            if self.terminal_reached is None:
                self.terminal_reached = target_state
            else:
                # already terminal and yet a further transition was
                # accepted -- I1 has been violated if this ever runs.
                raise AssertionError(
                    f"case moved from terminal {self.terminal_reached} to {target_state}"
                )

    @invariant()
    def terminal_is_sticky(self) -> None:
        if self.terminal_reached is not None:
            assert self.case.state == self.terminal_reached

    @invariant()
    def control_never_executes(self) -> None:
        if self.arm == Arm.CONTROL:
            assert self.case.state != CaseState.EXECUTING


TestCaseStateMachine = CaseStateMachine.TestCase
