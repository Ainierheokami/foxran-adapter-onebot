import sys
from pathlib import Path
import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.ban import BanTool
from tools.delete_msg import DeleteMsgTool
from tools.kick import KickTool
from tools.poke import PokeTool
from tools.read_forward_msg import ReadForwardMsgTool
from tools.set_group_card import SetGroupCardTool


def test_action_tools_are_user_visible_data_results():
    for tool_class in (BanTool, DeleteMsgTool, KickTool, PokeTool, SetGroupCardTool):
        contract = tool_class().get_execution_contract({})
        assert contract == {
            "output_kind": "data",
            "visibility": "user_visible",
            "continuation": "auto",
        }


def test_forward_reader_continues_agent_with_internal_data():
    contract = ReadForwardMsgTool().get_execution_contract({"message_id": "123"})
    assert contract == {
        "output_kind": "data",
        "visibility": "agent_only",
        "continuation": "continue_agent",
    }


@pytest.mark.asyncio
async def test_poke_tool_execute_success_has_no_verbose_message():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    tool = PokeTool()
    session_ctx = SimpleNamespace(
        session_notes={"onebot_target": {"group_id": "123456"}},
        websocket=object(),
    )
    with patch("app.adapters.onebot_v11.store.action_tracker.onebot_action_tracker.request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = {"status": "ok"}
        result = await tool.execute(user_id="7890", session_ctx=session_ctx)
        assert result.success is True
        assert result.data == ""

