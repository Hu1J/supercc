import pytest
from supercc.adapter.feishu.client import IncomingMessage
from supercc.adapter.feishu.core_protocol import incoming_to_inbound
from supercc.core.protocol import SessionKey, MessageRole, MessageType


class TestIncomingToInbound:
    def test_basic_conversion(self):
        incoming = IncomingMessage(
            message_id="msg_123",
            chat_id="oc_abc",
            user_open_id="ou_user1",
            content="Hello world",
            message_type="text",
            create_time="",
            parent_id="",
            thread_id="",
            raw_content="{}",
            is_group_chat=False,
            chat_type="p2p",
            mention_bot=False,
            mention_ids=[],
            group_name="",
        )
        inbound = incoming_to_inbound(
            incoming,
            bot_id="cli_xxx",
            project_path="/test/project",
        )
        assert inbound.message_id == "msg_123"
        assert inbound.content == "Hello world"
        assert inbound.role == MessageRole.USER
        assert inbound.session_key.bot_id == "cli_xxx"
        assert inbound.session_key.chat_id == "oc_abc"
        assert inbound.session_key.platform == "feishu"

    def test_session_key_four_keys(self):
        incoming = IncomingMessage(
            message_id="msg_456",
            chat_id="oc_group",
            user_open_id="ou_user2",
            content="hello",
            message_type="text",
            create_time="",
            parent_id="",
            thread_id="",
            raw_content="{}",
            is_group_chat=True,
            chat_type="group",
            mention_bot=True,
            mention_ids=["ou_bot1"],
            group_name="Test Group",
        )
        inbound = incoming_to_inbound(
            incoming,
            bot_id="bot_abc",
            project_path="/my/project",
        )
        key = inbound.session_key
        assert key.bot_id == "bot_abc"
        assert key.project_path == "/my/project"
        assert key.platform == "feishu"
        assert key.chat_id == "oc_group"

    def test_group_chat_extra_preserved(self):
        incoming = IncomingMessage(
            message_id="msg_789",
            chat_id="oc_group2",
            user_open_id="ou_user3",
            content="who is there",
            message_type="text",
            create_time="",
            parent_id="",
            thread_id="thread_abc",
            raw_content='{"key": "value"}',
            is_group_chat=True,
            chat_type="group",
            mention_bot=True,
            mention_ids=["ou_bot1", "ou_bot2"],
            group_name="My Group",
        )
        inbound = incoming_to_inbound(incoming, bot_id="b", project_path="/p")
        extra = inbound.extra
        assert extra["is_group_chat"] is True
        assert extra["mention_bot"] is True
        assert extra["mention_ids"] == ["ou_bot1", "ou_bot2"]
        assert extra["group_name"] == "My Group"
        assert extra["chat_type"] == "group"
        assert inbound.thread_id == "thread_abc"
        assert inbound.user_open_id == "ou_user3"

    def test_message_type_mapping(self):
        for msg_type, expected in [
            ("text", MessageType.TEXT),
            ("image", MessageType.IMAGE),
            ("file", MessageType.FILE),
            ("audio", MessageType.FILE),
            ("unknown", MessageType.TEXT),
        ]:
            incoming = IncomingMessage(
                message_id="x",
                chat_id="oc_y",
                user_open_id="ou_z",
                content="test",
                message_type=msg_type,
                create_time="",
            )
            inbound = incoming_to_inbound(incoming, bot_id="b", project_path="/p")
            assert inbound.message_type == expected, f"msg_type={msg_type!r}"

    def test_empty_user_open_id(self):
        incoming = IncomingMessage(
            message_id="msg_empty",
            chat_id="oc_test",
            user_open_id="",
            content="hello",
            message_type="text",
            create_time="",
        )
        inbound = incoming_to_inbound(incoming, bot_id="b", project_path="/p")
        assert inbound.user_open_id is None
