import sys
from pathlib import Path


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
