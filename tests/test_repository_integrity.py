from __future__ import annotations

import ast
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]

LOCAL_ROOTS = {
    p.name
    for p in ROOT.iterdir()
    if p.is_dir() and not p.name.startswith(".")
}
LOCAL_MODULES = {
    p.stem
    for p in ROOT.glob("*.py")
    if not p.name.startswith("_")
}
EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "node_modules", "build", "dist",
    "__pycache__", ".pytest_cache",
}


def _all_python_files():
    for path in ROOT.rglob("*.py"):
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        yield path


def _module_path(module: str) -> Path | None:
    parts = module.split(".")
    if not parts:
        return None
    root = parts[0]
    if root not in LOCAL_ROOTS and root not in LOCAL_MODULES:
        return None

    package_path = ROOT.joinpath(*parts)
    if package_path.with_suffix(".py").is_file():
        return package_path.with_suffix(".py")
    if (package_path / "__init__.py").is_file():
        return package_path / "__init__.py"
    return package_path


class RepositoryIntegrityTests(unittest.TestCase):
    def test_first_party_import_targets_exist(self):
        missing = []
        for path in _all_python_files():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:
                # compileall is the dedicated syntax gate; don't duplicate its report.
                continue

            package_parts = path.relative_to(ROOT).parts[:-1]
            package = list(package_parts)

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                    for module in modules:
                        target = _module_path(module)
                        if target is not None and not target.exists():
                            missing.append(f"{path.relative_to(ROOT)} -> import {module}")
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        base = package[: max(0, len(package) - node.level + 1)]
                        module = ".".join(base + ([node.module] if node.module else []))
                    else:
                        module = node.module or ""

                    if module:
                        target = _module_path(module)
                        if target is not None and not target.exists():
                            missing.append(f"{path.relative_to(ROOT)} -> from {module}")
        self.assertEqual(
            missing,
            [],
            "Broken first-party import targets:\n" + "\n".join(missing[:80]),
        )

    def test_runtime_version_constants_have_one_owner(self):
        owners = {
            "NODE_VERSION": [],
            "OMNIROUTE_VERSION": [],
            "OMNIROUTE_COMMIT": [],
            "PYTHON_MAJOR_MINOR": [],
            "PYTHON_BOOTSTRAP_VERSION": [],
        }
        pattern = re.compile(
            r"^\s*(NODE_VERSION|OMNIROUTE_VERSION|OMNIROUTE_COMMIT|"
            r"PYTHON_MAJOR_MINOR|PYTHON_BOOTSTRAP_VERSION)\s*=",
            re.MULTILINE,
        )
        for path in _all_python_files():
            if path.parts[-2:] == ("tests", path.name):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in pattern.finditer(text):
                owners[match.group(1)].append(str(path.relative_to(ROOT)))

        for name, paths in owners.items():
            self.assertEqual(
                paths,
                ["core/runtime_contract.py"],
                f"{name} must be owned only by core/runtime_contract.py; found {paths}",
            )

    def test_tool_declarations_are_reachable(self):
        main_path = ROOT / "main.py"
        tree = ast.parse(main_path.read_text(encoding="utf-8"), filename=str(main_path))

        declarations = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                if any(isinstance(t, ast.Name) and t.id == "TOOL_DECLARATIONS" for t in node.targets):
                    value = ast.literal_eval(node.value)
                    declarations = {
                        str(item["name"])
                        for item in value
                        if isinstance(item, dict) and item.get("name")
                    }
                    break
        self.assertTrue(declarations, "TOOL_DECLARATIONS could not be statically evaluated.")

        executor = next(
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_execute_tool"
        )
        reachable = set()

        for node in ast.walk(executor):
            if not isinstance(node, ast.Compare):
                continue
            if not (isinstance(node.left, ast.Name) and node.left.id == "name"):
                continue

            values = [node.left]
            for operand in node.comparators:
                values.append(operand)

            for op, operand in zip(node.ops, node.comparators):
                if isinstance(op, ast.Eq) and isinstance(operand, ast.Constant):
                    reachable.add(str(operand.value))
                elif isinstance(op, (ast.In, ast.NotIn)) and isinstance(operand, (ast.Tuple, ast.List, ast.Set)):
                    for item in operand.elts:
                        if isinstance(item, ast.Constant):
                            reachable.add(str(item.value))

        missing = sorted(declarations - reachable)
        self.assertEqual(
            missing,
            [],
            "Declared tools without a local executor branch: " + ", ".join(missing),
        )

    def test_conversation_delivery_contract_is_decoupled(self):
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        llm = (ROOT / "llm_client.py").read_text(encoding="utf-8")
        or_client = (ROOT / "or_client.py").read_text(encoding="utf-8")

        self.assertIn('name="text-command-agent"', main)
        self.assertIn("self.speak(reply, proactive=True, use_live=False)", main)
        self.assertIn("degraded_turn = bool(full_in) and not full_out and turn_audio_bytes < 256", main)
        self.assertIn("def _speak_native(self, text: str, profile)", main)
        self.assertIn("chat_with_tools(", main)
        self.assertIn("def chat_with_tools(", or_client)
        self.assertIn('normalize_provider(self._provider) == GEMINI', llm)

    def test_omniroute_has_one_runtime_owner_and_clean_shutdown(self):
        setup = (ROOT / "core" / "omniroute_setup.py").read_text(encoding="utf-8")
        gateway = (ROOT / "core" / "omniroute.py").read_text(encoding="utf-8")

        self.assertNotIn("class OmniRouteGateway", setup)
        self.assertEqual(len(re.findall(r"class OmniRouteGateway\b", gateway)), 1)
        self.assertIn("def stop(self)", setup)
        self.assertIn("def stop(self)", gateway)
        self.assertIn("atexit.register(_gateway.stop)", gateway)

    def test_no_stale_primary_python_runtime_in_documentation(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("Python 3.11", readme)


if __name__ == "__main__":
    unittest.main()
