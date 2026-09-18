import pytest

from conic.types.steering import (
    SteeringBackgroundResult,
    SteeringItem,
    SteeringStopCommand,
    SteeringUserMessage,
)


def test_steering_item_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        SteeringItem()


def test_steering_user_message_has_user_source():
    assert SteeringUserMessage("hi").source == "user"


def test_steering_user_message_only_takes_text():
    with pytest.raises(TypeError):
        SteeringUserMessage(text="hi", source="other")


def test_steering_user_message_is_not_turn_abort():
    assert SteeringUserMessage("hi").is_turn_abort() is False


def test_steering_user_message_to_history_entries():
    assert SteeringUserMessage("hi").to_history_entries() == [
        {"role": "user", "content": "hi"}
    ]


def test_steering_background_result_has_background_source():
    result = SteeringBackgroundResult(task_id="t1", exit_code=0, output="done")
    assert result.source == "background"


def test_steering_background_result_is_not_turn_abort():
    result = SteeringBackgroundResult(task_id="t1", exit_code=0, output="done")
    assert result.is_turn_abort() is False


def test_steering_background_result_to_history_entries_is_a_user_turn():
    result = SteeringBackgroundResult(task_id="t1", exit_code=0, output="done")
    entries = result.to_history_entries()
    assert len(entries) == 1
    assert entries[0]["role"] == "user"


def test_steering_background_result_to_history_entries_includes_task_id_and_output():
    result = SteeringBackgroundResult(task_id="t1", exit_code=0, output="done")
    content = result.to_history_entries()[0]["content"]
    assert "t1" in content
    assert "done" in content


def test_steering_stop_command_has_system_source():
    assert SteeringStopCommand().source == "system"


def test_steering_stop_command_is_turn_abort():
    assert SteeringStopCommand().is_turn_abort() is True


def test_steering_stop_command_to_history_entries_is_empty():
    assert SteeringStopCommand().to_history_entries() == []


def test_all_steering_items_are_steering_item_instances():
    assert isinstance(SteeringUserMessage("hi"), SteeringItem)
    assert isinstance(
        SteeringBackgroundResult(task_id="t1", exit_code=0, output="done"), SteeringItem
    )
    assert isinstance(SteeringStopCommand(), SteeringItem)
