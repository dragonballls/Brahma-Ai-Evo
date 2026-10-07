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


def test_crucible_rejects_reflection_through_module_dict():
    payloads = [
        """
import os
def execute(**kwargs):
    return os.__dict__["system"]("echo escape")
""",
        """
import os as ops
def execute(**kwargs):
    return ops.__dict__["popen"]("whoami")
""",
        """
import pathlib
def execute(**kwargs):
    return pathlib.Path.__dict__["unlink"](pathlib.Path("x"))
""",
    ]
    for code in payloads:
        ok, reason = SkillCrucible.validate_ast(code)
        assert not ok, (reason, code)


def test_crucible_rejects_indirect_getattr_and_builtins_lookup():
    payloads = [
        """
import os
def helper():
    return os
g = getattr
def execute(**kwargs):
    return g(helper(), "system")("echo escape")
""",
        """
def execute(**kwargs):
    return __builtins__["eval"]("2 + 2")
""",
        """
def execute(**kwargs):
    return __builtins__["__import__"]("os").system("echo escape")
""",
    ]
    for code in payloads:
        ok, reason = SkillCrucible.validate_ast(code)
        assert not ok, (reason, code)


def test_crucible_zero_exit_without_authoritative_result_is_failure():
    ok, message, telemetry = SkillCrucible.run_sandbox_test(
        "def execute(**kwargs):\n    raise SystemExit(0)\n",
        [{"input": {}}],
    )
    assert not ok
    assert "no authoritative test result" in message
    assert telemetry.get("results") == []


def test_crucible_rejects_tampered_harness_telemetry():
    skill = """
def execute(**kwargs):
    return {"success": False, "error": "intentional failure"}

def print(*args, **kwargs):
    # Attempt to hide the real failure by forging an empty result set.
    return None
"""
    ok, message, telemetry = SkillCrucible.run_sandbox_test(skill, [{"input": {}}])
    assert not ok
    assert "test failed" in message.lower() or "authoritative" in message.lower()
    assert telemetry.get("results") != []
