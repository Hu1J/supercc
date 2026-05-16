import pytest
from supercc.channels.wecom.core_protocol import incoming_to_inbound
from supercc.core.protocol import SessionKey, MessageRole, MessageType


class TestWeComIncomingToInbound:
    def test_basic_text_conversion(self):
        msg = {
            "msgid": "msg_123",
            "aibotid": "bot_abc",
            "chattype": "single",
            "chatid": "oc_user",
            "from": {"userid": "ou_user1"},
            "msgtype": "text",
            "text": {"content": "Hello world"},
        }
        inbound = incoming_to_inbound(msg, bot_id="cli_xxx", project_path="/test")
        assert inbound.message_id == "msg_123"
        assert inbound.content == "Hello world"
        assert inbound.role == MessageRole.USER
        assert inbound.session_key.bot_id == "cli_xxx"
        assert inbound.session_key.platform == "wecom"
        assert inbound.session_key.chat_id == "oc_user"

    def test_session_key_four_keys(self):
        msg = {
            "msgid": "msg_456",
            "aibotid": "bot_abc",
            "chattype": "group",
            "chatid": "oc_group2",
            "from": {"userid": "ou_user2"},
            "msgtype": "text",
            "text": {"content": "hello"},
        }
        inbound = incoming_to_inbound(msg, bot_id="bot_abc", project_path="/my/project")
        key = inbound.session_key
        assert key.bot_id == "bot_abc"
        assert key.project_path == "/my/project"
        assert key.platform == "wecom"
        assert key.chat_id == "oc_group2"
        assert key.chat_id != ""  # must be populated from chatid

    def test_image_message(self):
        msg = {
            "msgid": "img_001",
            "aibotid": "bot_x",
            "chattype": "single",
            "chatid": "oc_user",
            "from": {"userid": "ou_x"},
            "msgtype": "image",
            "image": {"media_id": "media123"},
        }
        inbound = incoming_to_inbound(msg, bot_id="b", project_path="/p")
        assert inbound.message_type == MessageType.IMAGE
        assert inbound.content == "[图片]"

    def test_file_message(self):
        msg = {
            "msgid": "file_001",
            "chattype": "single",
            "chatid": "oc_user",
            "from": {"userid": "ou_x"},
            "msgtype": "file",
            "file": {"media_id": "media456"},
        }
        inbound = incoming_to_inbound(msg, bot_id="b", project_path="/p")
        assert inbound.message_type == MessageType.FILE
        assert inbound.content == "[文件]"

    def test_group_chat_extra(self):
        msg = {
            "msgid": "grp_001",
            "chattype": "group",
            "chatid": "oc_group",
            "from": {"userid": "ou_member"},
            "msgtype": "text",
            "text": {"content": "hello group"},
        }
        inbound = incoming_to_inbound(msg, bot_id="b", project_path="/p")
        assert inbound.extra["is_group_chat"] is True
        assert inbound.extra["chat_type"] == "group"
        assert inbound.extra["mention_bot"] is False
