"""Cloud non-replay policy is independent of the Goal's display locale."""
import pytest

from app.schemas.provider import SAFE_PROVIDER_ERRORS
from app.session.goal_controller import GoalSliceResult, _generation_failure_kind


@pytest.mark.parametrize("code", list(SAFE_PROVIDER_ERRORS))
@pytest.mark.parametrize("language", [0, 1])
def test_fixed_cloud_error_never_reserves_another_goal_slice(code, language):
    message = SAFE_PROVIDER_ERRORS[code][language]
    result = GoalSliceResult(tokens_used=0, cost_used_microusd=0, active_seconds=1,
        finish_reason="error", total_cost=0, agent_error=message, agent_error_code=code)
    assert _generation_failure_kind(result, message) == "terminal"


def test_cloud_category_wins_over_retryable_exception_wording():
    result = GoalSliceResult(tokens_used=0, cost_used_microusd=0, active_seconds=1,
        finish_reason="error", total_cost=0, agent_error="Connection interrupted",
        agent_error_code="model_connection_interrupted", exception=ConnectionError("connection"))
    assert _generation_failure_kind(result, result.agent_error) == "terminal"


def test_noncloud_goal_retains_existing_network_recovery_policy():
    result = GoalSliceResult(tokens_used=0, cost_used_microusd=0, active_seconds=1,
        finish_reason="error", total_cost=0, agent_error="Connection interrupted")
    assert _generation_failure_kind(result, result.agent_error) == "retryable"
