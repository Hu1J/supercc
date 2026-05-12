"""Phase 2 端到端集成测试：FeishuWSClient → Core → FeishuClient"""

import pytest
import asyncio


class TestCoreComponentsIntegration:
    """验证核心组件能正确实例化和协作。"""

    def test_session_manager_and_worker_pool_creation(self):
        """验证 SessionManager 和 WorkerPool 能正常创建。"""
        from supercc.core.session import SessionManager
        from supercc.core.worker import WorkerPool

        sm = SessionManager(db_path=":memory:")
        pool = WorkerPool()

        assert sm is not None
        assert pool is not None

    def test_worker_pool_execute_signature(self):
        """验证 WorkerPool.execute() 方法签名正确。"""
        from supercc.core.worker import WorkerPool
        import inspect

        pool = WorkerPool()
        execute_method = pool.execute

        sig = inspect.signature(execute_method)
        params = list(sig.parameters.keys())
        assert "key" in params
        assert "session_id" in params
        assert "integration" in params
        assert "prompt" in params
        assert "on_stream" in params

    @pytest.mark.asyncio
    async def test_worker_pool_acquire_release(self):
        """验证 WorkerPool acquire/release 循环。"""
        from supercc.core.worker import WorkerPool
        from supercc.core.protocol import SessionKey

        pool = WorkerPool()
        key = SessionKey(
            bot_id="test_bot",
            project_path="/test",
            platform="feishu",
            chat_id="oc_test",
        )

        worker = await pool.acquire(key, "session_1", integration=None)
        assert worker.key == key
        assert worker.session_id == "session_1"

        stats = await pool.stats()
        assert stats["total_workers"] == 1

        await pool.release(key)
        stats = await pool.stats()
        assert stats["idle"] == 1

    @pytest.mark.asyncio
    async def test_core_executor_instantiation(self):
        """验证 CoreExecutor 能正常实例化。"""
        from supercc.core.session import SessionManager
        from supercc.core.worker import WorkerPool
        from supercc.core.executor import CoreExecutor

        sm = SessionManager(db_path=":memory:")
        pool = WorkerPool()
        executor = CoreExecutor(session_manager=sm, worker_pool=pool)

        assert executor.sessions is sm
        assert executor.pool is pool

    def test_feishu_core_ws_client_instantiation(self, tmp_path):
        """验证 FeishuCoreWSClient 能正常实例化。"""
        from supercc.adapter.feishu.core_client import FeishuCoreWSClient
        from supercc.adapter.feishu.client import FeishuClient

        # 创建临时目录
        data_dir = str(tmp_path / "data")
        tmp_path.mkdir(parents=True, exist_ok=True)

        # 创建 FeishuClient（只需要 app_id 和 app_secret）
        feishu = FeishuClient(
            app_id="test_app_id",
            app_secret="test_secret",
            bot_name="TestBot",
            data_dir=data_dir,
        )

        client = FeishuCoreWSClient(
            core_url="ws://127.0.0.1:8765",
            feishu_client=feishu,
            bot_id="cli_test",
            project_path="/test/project",
        )

        assert client.core_url == "ws://127.0.0.1:8765"
        assert client.bot_id == "cli_test"
        assert client.project_path == "/test/project"
        assert client._running is False

    def test_incoming_to_inbound_four_key_session(self):
        """验证 incoming_to_inbound 正确构建四元组 SessionKey。"""
        from supercc.adapter.feishu.client import IncomingMessage
        from supercc.adapter.feishu.core_protocol import incoming_to_inbound

        incoming = IncomingMessage(
            message_id="msg_test",
            chat_id="oc_group1",
            user_open_id="ou_user1",
            content="hello",
            message_type="text",
            create_time="",
        )

        inbound = incoming_to_inbound(
            incoming,
            bot_id="cli_abc",
            project_path="/my/project",
        )

        key = inbound.session_key
        assert key.bot_id == "cli_abc"
        assert key.project_path == "/my/project"
        assert key.platform == "feishu"
        assert key.chat_id == "oc_group1"

    def test_ws_server_accepts_executor(self):
        """验证 WsServer 能接受 executor 参数。"""
        from supercc.core.server import WsServer
        from supercc.core.session import SessionManager
        from supercc.core.worker import WorkerPool
        from supercc.core.executor import CoreExecutor
        import inspect

        # 检查 __init__ 签名
        sig = inspect.signature(WsServer.__init__)
        params = list(sig.parameters.keys())
        assert "executor" in params

        # 实际创建
        sm = SessionManager(db_path=":memory:")
        pool = WorkerPool()
        executor = CoreExecutor(session_manager=sm, worker_pool=pool)
        server = WsServer(host="127.0.0.1", port=18765, executor=executor)

        assert server._executor is executor

    @pytest.mark.asyncio
    async def test_worker_pool_permanent_binding(self):
        """验证 Worker 永久绑定：同一 key 获取同一个 Worker。"""
        from supercc.core.worker import WorkerPool
        from supercc.core.protocol import SessionKey

        pool = WorkerPool()
        key = SessionKey(
            bot_id="bot_x",
            project_path="/proj",
            platform="feishu",
            chat_id="oc_y",
        )

        w1 = await pool.acquire(key, "s1", integration="int_1")
        w2 = await pool.acquire(key, "s2", integration="int_2")

        # 同一个 key 应该返回相同的 Worker 实例
        assert w1 is w2
        # 新 integration 不应该被使用（永久绑定）
        assert w1.integration == "int_1"

        stats = await pool.stats()
        assert stats["total_workers"] == 1
