from core.skill_crucible import SkillCrucible


def _assert_rejected(source: str) -> None:
    ok, reason = SkillCrucible.validate_ast(source)
    assert not ok
    assert "subprocess" in (reason or "").lower()


def test_crucible_rejects_asyncio_subprocess_import():
    _assert_rejected(
        "import asyncio.subprocess\n"
        "async def execute(**kwargs):\n"
        "    return None\n"
    )


def test_crucible_rejects_asyncio_subprocess_helpers():
    _assert_rejected(
        "import asyncio\n"
        "async def execute(**kwargs):\n"
        "    return await asyncio.create_subprocess_exec('python', '-V')\n"
    )
    _assert_rejected(
        "from asyncio import create_subprocess_shell\n"
        "async def execute(**kwargs):\n"
        "    return await create_subprocess_shell('echo ok')\n"
    )


def test_crucible_rejects_object_graph_reflection_escape():
    from core.skill_crucible import SkillCrucible

    payloads = [
        """
def execute():
    return getattr(().__class__.__base__, "__subclasses__")
""",
        """
def execute():
    return ().__class__.__base__.__subclasses__()
""",
        """
def execute():
    return getattr(type, "__subclasses__")(type)
""",
        """
def execute():
    return getattr((lambda: None).__globals__, "__builtins__")
""",
    ]
    for code in payloads:
        ok, reason = SkillCrucible.validate_ast(code)
        assert not ok, (reason, code)
