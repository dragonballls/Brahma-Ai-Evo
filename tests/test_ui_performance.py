from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent.parent


def test_holographic_background_has_adaptive_frame_budget():
    source = (ROOT / "assets" / "web_background" / "index.html").read_text(encoding="utf-8")
    assert "let renderQuality = 0.82;" in source
    assert "function adaptRenderQuality(frameCostMs)" in source
    assert "frameTimeEma" in source
    assert "renderQuality = Math.max(0.65" in source
    assert "renderQuality = Math.min(1.0" in source
    assert "adaptRenderQuality(performance.now() - now);" in source


def test_holographic_background_reduces_cpu_geometry_work_at_lower_quality():
    source = (ROOT / "assets" / "web_background" / "index.html").read_text(encoding="utf-8")
    assert "const samples = renderQuality >= 0.95 ? 140 : renderQuality >= 0.75 ? 110 : 84;" in source
    assert "if (!item.line.visible) return;" in source
    assert "entry.line.geometry.setDrawRange(0, samples);" in source
    assert "standingWaveParticles.geometry.setDrawRange" in source


def test_holographic_background_reuses_starburst_vector():
    source = (ROOT / "assets" / "web_background" / "index.html").read_text(encoding="utf-8")
    assert "position: new THREE.Vector3()" in source
    assert "const pos = uData.position;" in source
    assert "const pos = new THREE.Vector3(lx, ly, 0);" not in source


def test_python_webengine_audio_bridge_is_throttled():
    source = (ROOT / "ui.py").read_text(encoding="utf-8")
    assert "0.10:  # 10 Hz keeps the Qt-WebEngine bridge responsive under load" in source


def test_background_quality_floor_preserves_visual_rendering():
    source = (ROOT / "assets" / "web_background" / "index.html").read_text(encoding="utf-8")
    match = re.search(r'renderQuality = Math\.max\((0\.65)', source)
    assert match and float(match.group(1)) >= 0.65
