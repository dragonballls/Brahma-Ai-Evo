import os
from pathlib import Path
import pytest
from core.globe_window import GlobeBridge

def test_globe_assets_exist():
    base_dir = Path(__file__).resolve().parent.parent
    assets_dir = base_dir / "assets" / "globe"
    html_file = assets_dir / "index.html"
    tex_file = assets_dir / "earth-night-2k.jpg"

    assert html_file.exists(), "assets/globe/index.html must exist"
    assert tex_file.exists(), "assets/globe/earth-night-2k.jpg must exist"
    assert tex_file.stat().st_size > 50000, "Optimized texture should be valid size"

def test_globe_html_performance_optimizations():
    base_dir = Path(__file__).resolve().parent.parent
    html_file = base_dir / "assets" / "globe" / "index.html"
    content = html_file.read_text(encoding="utf-8")

    # Verify texture loading
    assert "earth-night-2k.jpg" in content
    # Verify depthWrite false on atmosphere
    assert "depthWrite: false" in content
    # Verify dragging detection
    assert "isUserDragging" in content
    # Verify damping factor optimization
    assert "dampingFactor = 0.14" in content
    # Verify mediump precision for smooth rendering
    assert 'precision: "mediump"' in content
    # Verify GPU transform promotion in CSS
    assert "translateZ(0)" in content

def test_chromium_gpu_flags():
    assert "QTWEBENGINE_CHROMIUM_FLAGS" in os.environ
    flags = os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]
    assert "--enable-gpu-rasterization" in flags
    assert "--enable-zero-copy" in flags
    assert "--use-angle=d3d11" in flags

    experimental = os.environ.get("BRAHMA_EXPERIMENTAL_HOLO_RENDER") == "1"
    if experimental:
        assert "--ignore-gpu-blocklist" in flags
        assert "--disable-frame-rate-limit" in flags
        assert "--disable-gpu-vsync" in flags
    else:
        assert "--ignore-gpu-blocklist" not in flags
        assert "--disable-frame-rate-limit" not in flags
        assert "--disable-gpu-vsync" not in flags

def test_globe_bridge_signals():
    bridge = GlobeBridge()
    received = []

    bridge.close_requested.connect(lambda: received.append("closed"))
    bridge.view_updated.connect(lambda data: received.append(data))

    bridge.closeGlobe()
    assert "closed" in received

    bridge.reportView('{"centerLat": 20.5, "centerLon": 78.9}')
    assert any(isinstance(x, dict) and x.get("centerLat") == 20.5 for x in received)

def test_globe_low_end_device_optimizations():
    base_dir = Path(__file__).resolve().parent.parent
    html_file = base_dir / "assets" / "globe" / "index.html"
    content = html_file.read_text(encoding="utf-8")

    # Verify WebGL hardware fallback support for low-end GPUs
    assert "failIfMajorPerformanceCaveat: false" in content
    # Verify mipmap generation is disabled for fast upload without GPU stalls
    assert "tex.generateMipmaps = false" in content
    # Verify zero-allocation objects for GC smoothness
    assert "_camDirVec" in content
    assert "_pulseVec" in content
    # Verify lifecycle pause/resume
    assert "pauseGlobe" in content
    assert "resumeGlobe" in content
    assert "pause: () => pauseGlobe()" in content
    assert "resume: () => resumeGlobe()" in content
    # Verify heavy backdrop-filter is completely eliminated to protect Intel HD graphics
    assert "backdrop-filter" not in content
    # Verify pixel ratio is capped at 1.0
    assert "Math.min(window.devicePixelRatio || 1, 1.0)" in content
    # Verify responsive rotation speed & debounce
    assert "rotateSpeed = 1.25" in content
    assert "autoRotateSpeed = 2.0" in content
    assert "dragEndTimeout" in content
    assert "_flyStepQ" in content
    assert "status-fps-val" in content
    assert "lastFrameTime" in content

def test_globe_window_lifecycle_events():
    from core.globe_window import GlobeWindow
    assert hasattr(GlobeWindow, "showEvent")
    assert hasattr(GlobeWindow, "hideEvent")
