import pytest

from conic.core.errors import AbortTurn
from conic.core.messages import StepStart
from conic.plugins.policy.step_limit import StepLimitPlugin


async def test_allows_steps_under_the_limit():
    plugin = StepLimitPlugin(max_steps=3)
    await plugin.check(StepStart(step_index=0))
    await plugin.check(StepStart(step_index=2))


async def test_aborts_when_step_index_reaches_limit():
    plugin = StepLimitPlugin(max_steps=3)
    with pytest.raises(AbortTurn):
        await plugin.check(StepStart(step_index=3))
