def test_command_result_types():
    from supercc.core.commands.base import CommandResult, CommandCard
    r = CommandResult(content="hello")
    assert r.content == "hello"
    assert r.card is None

def test_command_handler_base():
    from supercc.core.commands.base import CommandHandler
    import inspect
    assert inspect.isclass(CommandHandler)
    assert hasattr(CommandHandler, 'execute')