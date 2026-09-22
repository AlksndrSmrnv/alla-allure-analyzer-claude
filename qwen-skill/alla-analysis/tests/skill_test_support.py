from alla_skill.models.common import TestStatus as Status
from alla_skill.models.testops import FailedTestSummary, ExecutionStep


def make_failed_test_summary(**overrides):
    return FailedTestSummary(
        **{"test_result_id": 1, "name": "test_example", "status": Status.FAILED, **overrides}
    )


def make_execution_step(**overrides):
    return ExecutionStep.model_validate(overrides)
