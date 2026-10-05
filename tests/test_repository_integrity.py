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
    def read(self, relative_path: str) -> str:
        return (ROOT / relative_path).read_text(encoding="utf-8", errors="replace")

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
                owners[match.group(1)].append(path.relative_to(ROOT).as_posix())

        for name, paths in owners.items():
            self.assertEqual(
                paths,
                [(ROOT / "core" / "runtime_contract.py").relative_to(ROOT).as_posix()],
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

    def test_dynamic_skills_share_the_runtime_tool_declaration_surface(self):
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("def _runtime_tool_declarations()", main)
        self.assertIn("DynamicToolRegistry.get_tool_declarations()", main)
        self.assertIn("tools=_runtime_tool_declarations()", main)
        self.assertIn("for declaration in DynamicToolRegistry.get_tool_declarations():", main)

    def test_executor_only_capabilities_are_declared_to_the_models(self):
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        declaration_end = main.find("\n]\n\n\nclass BrahmaLive")
        declarations = main[:declaration_end]
        for name in ("ram_hogs", "kill_process", "brightness_control", "rollback", "universal_task"):
            self.assertIn(f'"name": "{name}"', declarations)
    def test_conversation_delivery_contract_is_decoupled(self):
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        llm = (ROOT / "llm_client.py").read_text(encoding="utf-8")
        or_client = (ROOT / "or_client.py").read_text(encoding="utf-8")

        self.assertIn('name="text-command-agent"', main)
        self.assertIn("self.speak(reply, proactive=True, use_live=False)", main)
        self.assertIn("degraded_turn = bool(full_in) and not full_out and not had_usable_audio", main)
        self.assertIn("def _speak_native(self, text: str, profile)", main)
        self.assertIn("chat_with_tools(", main)
        self.assertIn("def chat_with_tools(", or_client)
        self.assertIn('normalize_provider(self._provider) == GEMINI', llm)

    def test_pyinstaller_includes_offline_speech_dependencies(self):
        source = (ROOT / "installer" / "BrahmaEvo.spec").read_text(encoding="utf-8")
        self.assertIn("'speech_recognition'", source)
        self.assertIn("'pocketsphinx'", source)

    def test_pyinstaller_datas_use_two_part_entries(self):
        source = (ROOT / "installer" / "BrahmaEvo.spec").read_text(encoding="utf-8")
        tree = ast.parse(source, filename="installer/BrahmaEvo.spec")

        analysis_call = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Analysis"
        )
        datas = next(
            keyword.value
            for keyword in analysis_call.keywords
            if keyword.arg == "datas"
        )
        self.assertIsInstance(datas, ast.BinOp)
        self.assertIsInstance(datas.left, ast.List)

        invalid = []
        for index, item in enumerate(datas.left.elts):
            if not isinstance(item, (ast.Tuple, ast.List)) or len(item.elts) != 2:
                invalid.append(index)
        self.assertEqual(
            invalid,
            [],
            "installer/BrahmaEvo.spec contains invalid PyInstaller datas entries at indexes: "
            + ", ".join(map(str, invalid)),
        )
        self.assertIn(
            "(os.path.join(cwd, 'config', 'models'), 'config/models')",
            source,
        )
        self.assertIn(
            "(os.path.join(cwd, 'config', 'intelligence.json'), 'config')",
            source,
        )

    def test_spotify_mcp_package_entrypoint_matches_build_output(self):
        package = ROOT / "actions" / "spotify_mcp_server" / "package.json"
        data = __import__("json").loads(package.read_text(encoding="utf-8"))
        self.assertEqual(data.get("main"), "build/index.js")
        self.assertTrue((package.parent / "src" / "index.ts").is_file())
        self.assertEqual(
            data.get("bin", {}).get("spotify-mcp"),
            "./build/index.js",
        )

    def test_omniroute_has_one_runtime_owner_and_clean_shutdown(self):
        setup = (ROOT / "core" / "omniroute_setup.py").read_text(encoding="utf-8")
        gateway = (ROOT / "core" / "omniroute.py").read_text(encoding="utf-8")

        self.assertNotIn("class OmniRouteGateway", setup)
        self.assertEqual(len(re.findall(r"class OmniRouteGateway\b", gateway)), 1)
        self.assertIn("def stop(self)", setup)
        self.assertIn("def stop(self)", gateway)
        self.assertIn("atexit.register(_gateway.stop)", gateway)

    def test_direct_google_genai_usage_is_limited_to_specialized_transports(self):
        allowed = {
            "core/gemini_runtime.py",
            "main.py",
            "actions/screen_processor.py",
            "actions/meeting_assistant.py",
            "actions/video_understanding.py",
            "actions/web_search.py",
        }
        offenders = []
        for path in _all_python_files():
            rel = path.relative_to(ROOT).as_posix()
            if rel.startswith("tests/"):
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            if (
                ("from google import genai" in source or "from google.genai import" in source)
                and rel not in allowed
            ):
                offenders.append(rel)
        self.assertEqual(
            offenders,
            [],
            "Direct google-genai usage must stay limited to canonical/specialized transports: "
            + ", ".join(offenders),
        )

    def test_discord_optional_dependency_logging_is_initialized_before_use(self):
        source = self.read("discord_bot.py")
        self.assertLess(
            source.index('logger = logging.getLogger("brahma_evo.discord")'),
            source.index("discord = _load_discord_module()"),
        )
        self.assertIn("from llm_client import client as unified_cloud_client", source)
        self.assertNotIn("API_KEYS_FILE = API_CONFIG_PATH", source)
        self.assertNotIn("genai.Client(", source)

    def test_legacy_google_generativeai_sdk_is_not_used(self):
        offenders = []
        for path in _all_python_files():
            if path.parent.name == "tests":
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            if "google.generativeai" in source or "GenerativeModel(" in source:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(
            offenders,
            [],
            "Legacy google.generativeai/GenerativeModel usage remains in: " + ", ".join(offenders),
        )

    def test_live_api_key_paths_are_not_reimplemented_in_feature_modules(self):
        offenders = []
        allowed = {
            ROOT / "config" / "__init__.py",
            ROOT / "core" / "runtime_paths.py",
        }
        for path in _all_python_files():
            if path.parent.name == "tests" or path in allowed:
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            if "api_keys.json" in source:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(
            offenders,
            [],
            "Feature modules must use the canonical config accessor/path: " + ", ".join(offenders),
        )

    def test_legacy_agent_command_adapter_exists(self):
        source = (ROOT / "actions" / "cmd_control.py").read_text(encoding="utf-8")
        planner = (ROOT / "agent" / "planner.py").read_text(encoding="utf-8")
        executor = (ROOT / "agent" / "executor.py").read_text(encoding="utf-8")
        self.assertIn("def cmd_control(", source)
        self.assertIn("cmd_control", planner)
        self.assertIn("from actions.cmd_control import cmd_control", executor)

    def test_no_stale_primary_python_runtime_in_documentation(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("Python 3.11", readme)

    def test_agent_ai_stack_uses_unified_provider_route(self):
        planner = self.read("agent/planner.py")
        executor = self.read("agent/executor.py")
        handler = self.read("agent/error_handler.py")

        for name, source in (
            ("agent/planner.py", planner),
            ("agent/executor.py", executor),
            ("agent/error_handler.py", handler),
        ):
            self.assertNotIn("from google import genai", source, name)
            self.assertNotIn("google.genai", source, name)
            self.assertNotIn("generate_content(", source, name)
            self.assertNotIn("core.gemini_runtime import", source, name)

        self.assertIn("from llm_client import client as unified_client", planner)
        self.assertIn("from llm_client import client as unified_client", executor)
        self.assertIn("from llm_client import client as unified_client", handler)
        self.assertIn('model="auto"', planner)
        self.assertIn('model="auto"', executor)
        self.assertIn('model="auto"', handler)

    def test_auto_heal_does_not_auto_promote_to_main(self):
        source = self.read("core/repository_sync.py")
        self.assertIn('BRAHMA_AUTO_PUBLISH_REPAIRS", "0"', source)
        self.assertIn("Auto-heal may repair the local runtime, but promotion to main is explicit.", source)

    def test_auto_heal_uses_canonical_cloud_route(self):
        source = (ROOT / "actions" / "auto_heal_engine.py").read_text(encoding="utf-8")
        self.assertIn("from llm_client import client as unified_client", source)
        self.assertNotIn("from google import genai", source)
        self.assertNotIn("g_client.models.generate_content(", source)

    def test_holographic_renderer_assets_stay_synchronized(self):
        desktop = (ROOT / "assets" / "web_background" / "index.html").read_bytes()
        android = (
            ROOT / "brahma-connect-android" / "app" / "src" / "main"
            / "assets" / "web_background" / "index.html"
        ).read_bytes()
        self.assertEqual(
            desktop,
            android,
            "Desktop and Android holographic renderer assets must stay byte-for-byte synchronized.",
        )

    def test_omniroute_slow_sync_does_not_use_gateway_state_lock(self):
        source = (ROOT / "core" / "omniroute.py").read_text(encoding="utf-8")
        self.assertIn("self._credentials_lock = threading.Lock()", source)
        self.assertIn("with self._credentials_lock:", source)
        self.assertIn("Credential registration can launch slow subprocesses", source)
        self.assertIn("if sync_needed:", source)


    def test_android_compose_icon_dependency_matches_usage(self):
        source = self.read("brahma-connect-android/app/build.gradle.kts")
        self.assertIn('implementation("androidx.compose.material:material-icons-core")', source)
        for relative in (
            "brahma-connect-android/app/src/main/java/com/brahma/connect/ui/BrahmaConnectApp.kt",
            "brahma-connect-android/app/src/main/java/com/brahma/connect/ui/ChatScreen.kt",
        ):
            kotlin = self.read(relative)
            if "androidx.compose.material.icons." in kotlin:
                self.assertIn(
                    'implementation("androidx.compose.material:material-icons-core")',
                    source,
                )

    def test_setup_prefers_binary_wheels_and_only_installs_chromium_fallback(self):
        bootstrap = self.read("bootstrap.ps1")
        setup = self.read("setup.py")
        self.assertIn("--prefer-binary -r requirements.txt", bootstrap)
        self.assertIn('"-m", "playwright", "install", "chromium"', bootstrap)
        self.assertIn('"--prefer-binary", "-r", "requirements.txt"', setup)
        self.assertIn('"playwright", "install", "chromium"', setup)
        self.assertNotIn('"playwright", "install"],', setup)

    def test_bootstrap_caches_and_validates_runtime_installers(self):
        source = self.read("bootstrap.ps1")
        self.assertIn('BrahmaAI\\\\downloads', source)
        self.assertIn('$tempPythonInstaller = "$PythonInstaller.download"', source)
        self.assertIn('$tempNodeInstaller = "$NodeInstaller.download"', source)
        self.assertIn('Move-Item -Force $tempPythonInstaller $PythonInstaller', source)
        self.assertIn('Move-Item -Force $tempNodeInstaller $NodeInstaller', source)
        self.assertIn('Using cached Python installer', source)
        self.assertIn('Using cached Node installer', source)

    def test_bootstrap_validates_existing_venv_python_version(self):
        source = self.read("bootstrap.ps1")
        self.assertIn("$VenvNeedsRecreate = -not (Test-Path $VenvPython)", source)
        self.assertIn("$VenvVersionCheck = & $VenvPython -c", source)
        self.assertIn("Existing .venv uses a different Python major/minor; recreating it.", source)
        self.assertIn("if (-not (Test-Path $VenvPython))", source)

    def test_source_launcher_routes_through_repair_bootstrap(self):
        source = self.read("start_brahma.vbs")
        bootstrap_pos = source.index("ElseIf fso.FileExists(bootstrap) And fso.FileExists(mainPy) Then")
        fallback_pos = source.index("ElseIf fso.FileExists(venvPython)", bootstrap_pos)
        self.assertGreaterEqual(bootstrap_pos, 0)
        self.assertGreater(fallback_pos, bootstrap_pos)
        self.assertIn("-WindowStyle Hidden -File", source)
        self.assertIn('shell.Run Chr(34) & powershell & Chr(34)', source[bootstrap_pos:fallback_pos])

    def test_ui_launcher_uses_active_interpreter_not_machine_specific_path(self):
        source = self.read("ui.py")
        self.assertNotIn(r"C:\\Users\\ravit\\AppData\\Local\\Programs\\Python\\Python313", source)
        self.assertIn("python.with_name(\"pythonw.exe\")", source)

    def test_windows_ui_powershell_launches_hide_console_windows(self):
        source = self.read("ui.py")
        self.assertGreaterEqual(
            source.count('creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if _OS == "Windows" else 0'),
            2,
            "UI PowerShell helpers must explicitly hide Windows console windows.",
        )


if __name__ == "__main__":
    unittest.main()
