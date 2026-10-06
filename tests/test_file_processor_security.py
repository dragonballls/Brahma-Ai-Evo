from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_archive_extraction_rejects_traversal_links_and_size_abuse():
    source = (ROOT / "actions" / "file_processor.py").read_text(encoding="utf-8")
    assert "def _safe_archive_target" in source
    assert "path-traversal member" in source
    assert "Archive symlink members are not allowed." in source
    assert "Archive link members are not allowed." in source
    assert "MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024" in source
    assert "def preflight(members)" in source
    assert 'target.open("xb")' in source
    assert "Archive would overwrite an existing path" in source


def test_video_transcription_uses_exclusive_temp_creation():
    source = (ROOT / "actions" / "file_processor.py").read_text(encoding="utf-8")
    assert "tempfile.mkstemp" in source
    assert "tempfile.mktemp" not in source


def test_archive_preflight_rejects_file_directory_path_conflicts():
    source = (ROOT / "actions" / "file_processor.py").read_text(encoding="utf-8")
    start = source.index("def preflight(members)")
    end = source.index("\n\n    if path.suffix.lower() == \".zip\"", start)
    block = source[start:end]
    assert "seen_types: dict[Path, bool]" in block
    assert "file/directory path conflict" in block
    assert "return [(member, target) for member, target, _ in planned]" in block


def test_ffmpeg_processing_checks_process_exit_codes_before_success():
    source = (ROOT / "actions" / "file_processor.py").read_text(encoding="utf-8")
    start = source.index("def _process_video")
    block = source[start:source.index("def _safe_archive_target", start)]
    assert "returncode == 0" in block
    assert block.count("if result.returncode != 0:") >= 6


def test_ffmpeg_operations_report_failure_instead_of_success():
    source = (ROOT / "actions" / "file_processor.py").read_text(encoding="utf-8")
    for phrase in (
        "Extract audio failed (ffmpeg exit",
        "Trim failed (ffmpeg exit",
        "Extract frame failed (ffmpeg exit",
        "Compress failed (ffmpeg exit",
        "Video transcription failed (ffmpeg exit",
        "Convert failed (ffmpeg exit",
    ):
        assert phrase in source

def test_file_processor_confines_input_paths_and_avoids_overwrite():
    source = (ROOT / "actions" / "file_processor.py").read_text(encoding="utf-8")
    assert "def _resolve_input_path" in source
    assert "File processing is limited to paths inside the user's home directory." in source
    assert "if not base.exists():" in source
    assert 'f"{src.stem}_{suffix}_{counter}{ext}"' in source


def test_file_processor_python_execution_requires_confirmation_and_checks_exit_code():
    source = (ROOT / "actions" / "file_processor.py").read_text(encoding="utf-8")
    assert "def _run_python_with_confirmation" in source
    assert "from core.confirm import request" in source
    assert 'key=f"file-execute:{path}"' in source
    assert "Python execution failed (exit" in source
    assert "Python execution timed out after 30 seconds." in source
    assert 'return _run_python_with_confirmation(path)' in source


def test_archive_extraction_destination_is_home_confined_and_symlink_free():
    source = (ROOT / "actions" / "file_processor.py").read_text(encoding="utf-8")
    assert "def _safe_archive_destination" in source
    assert "Archive extraction is limited to destinations inside the user's home directory." in source
    assert "Archive extraction destinations may not contain symlinked path components." in source
    assert "_safe_archive_destination(" in source


def test_file_processor_bounds_large_inputs_and_document_dimensions():
    source = (ROOT / "actions" / "file_processor.py").read_text(encoding="utf-8")
    assert "MAX_INPUT_BYTES = 128 * 1024 * 1024" in source
    assert "MAX_DOCUMENT_PAGES = 500" in source
    assert "MAX_PRESENTATION_SLIDES = 500" in source
    assert "MAX_IMAGE_DIMENSION = 12000" in source
    assert "Input file exceeds the" in source
    assert "PDF exceeds the {MAX_DOCUMENT_PAGES}-page processing limit." in source
    assert "Presentation exceeds the {MAX_PRESENTATION_SLIDES}-slide processing limit." in source
    assert "Image dimensions exceed the {MAX_IMAGE_DIMENSION}px safety limit." in source
