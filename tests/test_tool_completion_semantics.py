import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_tool_execution_initializes_result_and_shutdown_result():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    start = source.index("    async def _execute_tool")
    block = source[start:source.index("    async def _serve_dashboard", start)]
    assert "result = \"\"" in block
    assert 'result = "Shutdown initiated."' in block


def test_tool_completion_detects_explicit_failure_results():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(node for node in ast.walk(tree)
              if isinstance(node, ast.FunctionDef) and node.name == "_action_result_is_failure")
    namespace = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), namespace)
    checker = namespace["_action_result_is_failure"]

    assert checker({"success": False}) is True
    assert checker({"ok": False}) is True
    assert checker("failed to launch") is True
    assert checker("Unable to connect") is True
    assert checker({"success": True, "message": "zero results found"}) is False

def test_main_execute_tool_has_strict_runtime_result_gate():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    start = source.index("async def _execute_tool")
    block = source[start:source.index("async def _serve_dashboard", start)]
    assert "def _require_runtime_result" in block
    assert "value is None or value is False" in block
    assert "value.get(" in block
    assert "result = r or" not in block



def test_game_updater_distinguishes_update_request_from_verified_completion():
    from unittest.mock import patch
    from actions import game_updater

    steam_path = Path("C:/Steam")
    games = [{"id": "123", "name": "Demo Game", "state": 0}]
    with (
        patch.object(game_updater, "_ensure_steam_running", return_value=True),
        patch.object(game_updater, "_get_steam_games", return_value=games),
        patch.object(game_updater.subprocess, "Popen") as popen,
        patch.object(game_updater.time, "sleep", return_value=None),
    ):
        result = game_updater._update_steam_games(steam_path, "Demo Game")

    popen.assert_called_once_with([str(steam_path / "steam.exe"), "steam://update/123"])
    assert "update request launched" in result.casefold()
    assert "not independently verified" in result.casefold()
    assert "update started for" not in result.casefold()
