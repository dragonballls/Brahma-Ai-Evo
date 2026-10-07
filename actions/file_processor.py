"""
file_processor.py — Brahma AI Universal File Processor

Supported types:
  image   → describe, ocr, resize, convert, compress, crop
  pdf     → summarize, extract_text, extract_pages, to_word
  docx    → summarize, extract_text, reformat, translate_hint
  txt/md  → summarize, reformat, translate_hint, word_count
  csv     → analyze, filter, sort, convert, stats
  xlsx    → analyze, filter, convert, stats
  json    → validate, format, extract, convert
  code    → explain, review, fix, run, document
  audio   → transcribe, trim, convert, info
  video   → trim, extract_audio, extract_frame, info, compress
  zip     → list, extract
  pptx    → summarize, extract_text, to_pdf
"""

import os
import re
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime

_WINFS = None
if os.name == "nt":
    from core import windows_file_safety as _WINFS

from core.gemini_runtime import create_model, get_api_key

MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_DOCUMENT_PAGES = 500
MAX_PRESENTATION_SLIDES = 500
MAX_IMAGE_DIMENSION = 12000


def _get_api_key() -> str:
    return get_api_key()


def _gemini_client():
    """Return the Gemini adapter only when Gemini is the explicitly selected provider."""
    from core.provider_policy import require_provider

    require_provider("Gemini", "File AI analysis/transcription")
    return create_model("gemini-3.8-flash")


def _detect_type(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    image_exts = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "svg", "ico"}
    video_exts = {"mp4", "avi", "mov", "mkv", "wmv", "flv", "webm", "m4v", "3gp"}
    audio_exts = {"mp3", "wav", "ogg", "m4a", "aac", "flac", "wma", "opus"}
    code_exts  = {"py", "js", "ts", "jsx", "tsx", "html", "css", "java", "c",
                  "cpp", "cs", "go", "rs", "rb", "php", "swift", "kt", "sh",
                  "bash", "ps1", "lua", "r", "m", "sql", "yaml", "toml"}
    archive_exts = {"zip", "rar", "tar", "gz", "7z", "bz2", "xz"}

    if ext in image_exts:  return "image"
    if ext in video_exts:  return "video"
    if ext in audio_exts:  return "audio"
    if ext in code_exts:   return "code"
    if ext in archive_exts: return "archive"
    if ext == "pdf":       return "pdf"
    if ext in ("docx", "doc"): return "docx"
    if ext in ("txt", "md", "rst", "log"): return "text"
    if ext in ("csv", "tsv"): return "csv"
    if ext in ("xlsx", "xls", "ods"): return "excel"
    if ext == "json":      return "json"
    if ext == "xml":       return "xml"
    if ext in ("pptx", "ppt"): return "pptx"
    return "unknown"


def _file_size_str(path: Path) -> str:
    size = path.stat().st_size
    if size < 1024:        return f"{size} B"
    if size < 1024**2:     return f"{size/1024:.1f} KB"
    if size < 1024**3:     return f"{size/1024**2:.1f} MB"
    return f"{size/1024**3:.1f} GB"


def _cleanup_generated_artifact(path: Path | None) -> None:
    if path is None:
        return
    try:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _validate_output_format(path: Path) -> None:
    """Validate lightweight structural invariants without requiring network access."""
    ext = path.suffix.casefold()
    if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".ico"}:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        return
    if ext == ".json":
        text = path.read_text(encoding="utf-8")
        json.loads(text)
        return
    if ext in {".docx", ".xlsx", ".pptx"}:
        import zipfile
        required = {
            ".docx": {"[Content_Types].xml", "word/document.xml"},
            ".xlsx": {"[Content_Types].xml", "xl/workbook.xml"},
            ".pptx": {"[Content_Types].xml", "ppt/presentation.xml"},
        }[ext]
        if not zipfile.is_zipfile(path):
            raise RuntimeError(f"Generated {ext[1:].upper()} output is not a valid OOXML package: {path}")
        with zipfile.ZipFile(path, "r") as archive:
            if archive.testzip() is not None:
                raise RuntimeError(f"Generated {ext[1:].upper()} output contains a corrupt archive member: {path}")
            names = set(archive.namelist())
            missing = required - names
            if missing:
                raise RuntimeError(
                    f"Generated {ext[1:].upper()} output is missing required package parts: {sorted(missing)}"
                )
        return
    if ext == ".pdf":
        header = path.read_bytes()[:5]
        if header != b"%PDF-":
            raise RuntimeError(f"Generated PDF output has an invalid header: {path}")
        return


def _verify_output_artifact(path: Path) -> Path:
    """Fail closed unless a newly generated file exists and has basic structural validity."""
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"Generated output is not a regular file: {path}")
        if path.stat().st_size <= 0:
            raise RuntimeError(f"Generated output is empty: {path}")
        _validate_output_format(path)
    except OSError as exc:
        _cleanup_generated_artifact(path)
        raise RuntimeError(f"Generated output could not be verified: {path}") from exc
    except RuntimeError:
        _cleanup_generated_artifact(path)
        raise
    except Exception as exc:
        _cleanup_generated_artifact(path)
        raise RuntimeError(f"Generated output format validation failed: {path}") from exc
    return path

def _output_path(src: Path, suffix: str, new_ext: str = None) -> Path:
    ext = new_ext or src.suffix
    base = src.parent / f"{src.stem}_{suffix}{ext}"
    if not base.exists():
        if base.is_symlink():
            raise RuntimeError(f"Refusing to use symlink output path: {base}")
        return base
    counter = 1
    while True:
        candidate = src.parent / f"{src.stem}_{suffix}_{counter}{ext}"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
        counter += 1

def _secure_write_new_text(target: Path, content: str) -> None:
    """Create a new text output without a check-then-open pathname race."""
    target = Path(target).absolute()
    if target.is_symlink():
        raise RuntimeError(f"Refusing to use symlink output path: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    if os.name == "nt" and _WINFS is not None:
        fd, _final, _info = _WINFS.open_safe_file(
            target,
            write=True,
            create_new=True,
            exclusive=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
                fd = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if fd >= 0:
                os.close(fd)
        return

    if hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"):
        root = Path(target.anchor) if target.anchor else Path(".").absolute()
        relative_parent = target.parent.relative_to(root)
        parent_fd = os.open(
            str(root),
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            for part in relative_parent.parts:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
                os.close(parent_fd)
                parent_fd = next_fd
            fd = os.open(
                target.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
        finally:
            os.close(parent_fd)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
                fd = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if fd >= 0:
                os.close(fd)
        return

    raise RuntimeError("Safe text output primitives are unavailable on this platform.")


def _process_image(path: Path, action: str, params: dict, speak=None) -> str:
    try:
        from PIL import Image
    except ImportError:
        return "Pillow is not installed. Run: pip install Pillow"

    action = action or "describe"

    if action in ("describe", "ocr", "analyze", "read", "extract_text"):
        try:
            model  = _gemini_client()
            img    = Image.open(path)
            prompt = {
                "describe": "Describe this image in detail.",
                "ocr":      "Extract all text visible in this image. Return only the text, formatted clearly.",
                "analyze":  "Analyze this image thoroughly: objects, colors, composition, any text, context.",
                "read":     "Read all text in this image, preserving structure and formatting.",
                "extract_text": "Extract all text from this image.",
            }.get(action, "Describe this image.")

            if params.get("instruction"):
                prompt = params["instruction"]

            response = model.generate_content([prompt, img])
            result   = response.text.strip()

            if len(result) > 500 and params.get("save", True):
                out = _output_path(path, "result", ".txt")
                _secure_write_new_text(out, result)
                _verify_output_artifact(out)
                return f"{result[:300]}...\n\nFull result saved to: {out}"
            return result
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"AI image analysis failed: {e}"

    if action == "resize":
        width  = int(params.get("width",  0))
        height = int(params.get("height", 0))
        scale  = float(params.get("scale", 0))
        try:
            img = Image.open(path)
            w, h = img.size
            if w > MAX_IMAGE_DIMENSION or h > MAX_IMAGE_DIMENSION:
                return f"Image dimensions exceed the {MAX_IMAGE_DIMENSION}px safety limit."
            if scale:
                new_size = (int(w * scale), int(h * scale))
            elif width and height:
                new_size = (width, height)
            elif width:
                new_size = (width, int(h * width / w))
            elif height:
                new_size = (int(w * height / h), height)
            else:
                return "Please specify width, height, or scale."
            out = _output_path(path, f"resized_{new_size[0]}x{new_size[1]}")
            img.resize(new_size, Image.LANCZOS).save(out)
            _verify_output_artifact(out)
            return f"Resized from {w}x{h} to {new_size[0]}x{new_size[1]}. Saved: {out.name}"
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"Resize failed: {e}"

    if action == "convert":
        fmt = params.get("format", "png").lower().strip(".")
        fmt_map = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG",
                   "webp": "WEBP", "bmp": "BMP", "tiff": "TIFF"}
        pil_fmt = fmt_map.get(fmt, fmt.upper())
        try:
            img = Image.open(path).convert("RGB") if fmt == "jpg" else Image.open(path)
            out = _output_path(path, "converted", f".{fmt}")
            img.save(out, pil_fmt)
            _verify_output_artifact(out)
            return f"Converted to {fmt.upper()}. Saved: {out.name}"
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"Convert failed: {e}"

    if action == "compress":
        quality = int(params.get("quality", 70))
        try:
            img = Image.open(path).convert("RGB")
            out = _output_path(path, f"compressed_q{quality}", ".jpg")
            img.save(out, "JPEG", quality=quality, optimize=True)
            before = _file_size_str(path)
            after  = _file_size_str(out)
            _verify_output_artifact(out)
            return f"Compressed: {before} → {after}. Saved: {out.name}"
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"Compress failed: {e}"

    if action == "info":
        try:
            img = Image.open(path)
            return (f"Image info: {img.format}, {img.size[0]}x{img.size[1]}px, "
                    f"mode: {img.mode}, size: {_file_size_str(path)}")
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"Info failed: {e}"

    return _process_image(path, "describe", {"instruction": f"{action}: {params}"})

def _process_pdf(path: Path, action: str, params: dict, speak=None) -> str:
    action = action or "summarize"

    def _extract_pdf_text(max_chars=50000) -> str:
        text = ""
        try:
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                if len(pdf.pages) > MAX_DOCUMENT_PAGES:
                    raise ValueError(f"PDF exceeds the {MAX_DOCUMENT_PAGES}-page processing limit.")
                for page in pdf.pages:
                    text += (page.extract_text() or "") + "\n"
        except ImportError:
            try:
                import PyPDF2
                with open(path, "rb") as f:
                    reader = PyPDF2.PdfReader(f)
                    if len(reader.pages) > MAX_DOCUMENT_PAGES:
                        raise ValueError(f"PDF exceeds the {MAX_DOCUMENT_PAGES}-page processing limit.")
                    for page in reader.pages:
                        text += page.extract_text() + "\n"
            except ImportError:
                return ""
        return text[:max_chars]

    if action in ("summarize", "extract_text", "translate_hint", "analyze", "reformat"):
        text = _extract_pdf_text()
        if not text.strip():
            return "Could not extract text from PDF (may be scanned/image-based)."

        if action == "extract_text":
            out = _output_path(path, "text", ".txt")
            _secure_write_new_text(out, text)
            _verify_output_artifact(out)
            return f"Text extracted ({len(text)} chars). Saved: {out.name}"

        prompt_map = {
            "summarize":      f"Summarize this PDF document concisely:\n\n{text}",
            "analyze":        f"Analyze this document thoroughly:\n\n{text}",
            "translate_hint": f"What language is this document in and what does it say? Summarize:\n\n{text}",
            "reformat":       f"Reformat this text cleanly with proper structure:\n\n{text}",
        }
        try:
            model    = _gemini_client()
            response = model.generate_content(prompt_map.get(action, f"Analyze:\n\n{text}"))
            result   = response.text.strip()
            if len(result) > 600 and params.get("save", True):
                out = _output_path(path, action, ".txt")
                _secure_write_new_text(out, result)
                _verify_output_artifact(out)
                return f"{result[:400]}...\n\nFull result saved: {out.name}"
            return result
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"AI analysis failed: {e}"

    if action == "info":
        try:
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                pages = len(pdf.pages)
            return f"PDF: {pages} pages, size: {_file_size_str(path)}"
        except Exception:
            return f"PDF size: {_file_size_str(path)}"

    if action == "to_word":
        text = _extract_pdf_text()
        if not text:
            return "Could not extract text to convert."
        try:
            from docx import Document
            doc  = Document()
            doc.add_heading(path.stem, 0)
            for para in text.split("\n\n"):
                if para.strip():
                    doc.add_paragraph(para.strip())
            out = _output_path(path, "converted", ".docx")
            doc.save(out)
            _verify_output_artifact(out)
            return f"Converted to Word document. Saved: {out.name}"
        except ImportError:
            return "python-docx not installed. Run: pip install python-docx"

    return f"Unknown PDF action: '{action}'. Try: summarize, extract_text, info, to_word"

def _process_text_doc(path: Path, file_type: str, action: str,
                       params: dict, speak=None) -> str:
    action = action or "summarize"

    def _read_content() -> str:
        if file_type == "docx":
            try:
                from docx import Document
                doc  = Document(path)
                return "\n".join(p.text for p in doc.paragraphs)
            except ImportError:
                return "python-docx not installed."
            except Exception as e:
                _cleanup_generated_artifact(locals().get("out"))
                return f"Read failed: {e}"
        else:
            return path.read_text(encoding="utf-8", errors="ignore")

    content = _read_content()
    if not content.strip():
        return "File appears to be empty."

    if action == "word_count":
        words = len(content.split())
        chars = len(content)
        lines = content.count("\n")
        return f"Word count: {words} words, {chars} characters, {lines} lines."

    if action == "extract_text":
        if file_type != "txt":
            out = _output_path(path, "extracted", ".txt")
            _secure_write_new_text(out, content)
            _verify_output_artifact(out)
            return f"Text extracted. Saved: {out.name}"
        return content[:2000]

    instruction = params.get("instruction", "")
    prompt_map  = {
        "summarize":  f"Summarize this document concisely:\n\n{content[:40000]}",
        "analyze":    f"Analyze this document:\n\n{content[:40000]}",
        "reformat":   f"Reformat this text with clean structure, proper headings and paragraphs:\n\n{content[:40000]}",
        "fix":        f"Fix grammar, spelling and style issues in this text:\n\n{content[:40000]}",
        "translate_hint": f"What language is this and what does it say? Summarize:\n\n{content[:10000]}",
        "to_bullet":  f"Convert this text into a clear bullet-point summary:\n\n{content[:40000]}",
        "custom":     f"{instruction}\n\n{content[:40000]}",
    }

    if action not in prompt_map:

        action  = "custom"
        instruction = action

    try:
        model    = _gemini_client()
        response = model.generate_content(prompt_map[action])
        result   = response.text.strip()
        if len(result) > 600 and params.get("save", True):
            out = _output_path(path, action, ".txt")
            _secure_write_new_text(out, result)
            _verify_output_artifact(out)
            return f"{result[:400]}...\n\nFull result saved: {out.name}"
        return result
    except Exception as e:
        _cleanup_generated_artifact(locals().get("out"))
        return f"AI processing failed: {e}"


def _process_data(path: Path, file_type: str, action: str,
                  params: dict, speak=None) -> str:
    try:
        import pandas as pd
    except ImportError:
        return "pandas not installed. Run: pip install pandas openpyxl"

    action = action or "analyze"

    try:
        if file_type == "csv":
            df = pd.read_csv(path, encoding="utf-8", errors="replace")
        else:
            df = pd.read_excel(path)
    except Exception as e:
        _cleanup_generated_artifact(locals().get("out"))
        return f"Could not read file: {e}"

    if action == "info":
        return (f"Rows: {len(df)}, Columns: {len(df.columns)}\n"
                f"Columns: {', '.join(df.columns.tolist())}\n"
                f"Size: {_file_size_str(path)}")

    if action == "stats":
        try:
            desc = df.describe(include="all").to_string()
            return f"Statistics:\n{desc[:2000]}"
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"Stats failed: {e}"

    if action == "analyze":
        preview = df.head(50).to_string()
        prompt  = (f"Analyze this dataset. Columns: {list(df.columns)}\n"
                   f"Rows: {len(df)}\nPreview:\n{preview}\n\n"
                   f"Give insights, patterns, and notable findings.")
        try:
            model    = _gemini_client()
            response = model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"AI analysis failed: {e}"

    if action in ("convert", "to_csv", "to_excel", "to_json"):
        fmt = {"to_csv": "csv", "to_excel": "xlsx", "to_json": "json",
               "convert": params.get("format", "csv")}.get(action, "csv")
        try:
            if fmt == "csv":
                out = _output_path(path, "converted", ".csv")
                df.to_csv(out, index=False, encoding="utf-8")
            elif fmt == "xlsx":
                out = _output_path(path, "converted", ".xlsx")
                df.to_excel(out, index=False)
            elif fmt == "json":
                out = _output_path(path, "converted", ".json")
                df.to_json(out, orient="records", force_ascii=False, indent=2)
            _verify_output_artifact(out)
            return f"Converted to {fmt.upper()}. Saved: {out.name}"
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"Convert failed: {e}"

    if action == "filter":
        col       = params.get("column", "")
        value     = params.get("value", "")
        condition = params.get("condition", "equals")
        if not col or col not in df.columns:
            return f"Column '{col}' not found. Available: {', '.join(df.columns)}"
        try:
            if condition == "equals":     filtered = df[df[col] == value]
            elif condition == "contains": filtered = df[df[col].astype(str).str.contains(str(value), case=False)]
            elif condition == "gt":       filtered = df[df[col] > float(value)]
            elif condition == "lt":       filtered = df[df[col] < float(value)]
            else:                         filtered = df[df[col] == value]
            out = _output_path(path, "filtered", ".csv")
            filtered.to_csv(out, index=False)
            _verify_output_artifact(out)
            return f"Filtered: {len(filtered)} rows match. Saved: {out.name}"
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"Filter failed: {e}"

    if action == "sort":
        col = params.get("column", df.columns[0])
        asc = params.get("ascending", True)
        try:
            sorted_df = df.sort_values(col, ascending=asc)
            out = _output_path(path, "sorted", path.suffix)
            sorted_df.to_csv(out, index=False)
            _verify_output_artifact(out)
            return f"Sorted by '{col}'. Saved: {out.name}"
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"Sort failed: {e}"

    preview = df.head(30).to_string()
    try:
        model    = _gemini_client()
        response = model.generate_content(
            f"Task: {action}\nDataset ({len(df)} rows, cols: {list(df.columns)}):\n{preview}"
        )
        return response.text.strip()
    except Exception as e:
        _cleanup_generated_artifact(locals().get("out"))
        return f"Processing failed: {e}"


def _process_json(path: Path, action: str, params: dict, speak=None) -> str:
    action = action or "analyze"
    try:
        content = path.read_text(encoding="utf-8")
        data    = json.loads(content)
    except Exception as e:
        _cleanup_generated_artifact(locals().get("out"))
        return f"Invalid JSON: {e}"

    if action == "validate":
        return f"Valid JSON. Type: {type(data).__name__}, size: {_file_size_str(path)}"

    if action == "format":
        out = _output_path(path, "formatted", ".json")
        _secure_write_new_text(out, json.dumps(data, indent=2, ensure_ascii=False))
        _verify_output_artifact(out)
        return f"Formatted JSON saved: {out.name}"

    if action in ("analyze", "summarize", "extract"):
        preview = json.dumps(data, indent=2, ensure_ascii=False)[:8000]
        prompt  = f"Task: {action} this JSON data:\n{preview}"
        if params.get("instruction"):
            prompt = f"{params['instruction']}\n\nJSON data:\n{preview}"
        try:
            model    = _gemini_client()
            response = model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"AI processing failed: {e}"

    if action == "to_csv":
        try:
            import pandas as pd
            if isinstance(data, list):
                df  = pd.DataFrame(data)
                out = _output_path(path, "converted", ".csv")
                df.to_csv(out, index=False)
                _verify_output_artifact(out)
                return f"Converted to CSV. Saved: {out.name}"
            return "JSON must be an array of objects to convert to CSV."
        except ImportError:
            return "pandas not installed."

    return _process_json(path, "analyze", {"instruction": action})

def _process_code(path: Path, action: str, params: dict, speak=None) -> str:
    action  = action or "explain"
    content = path.read_text(encoding="utf-8", errors="ignore")
    ext     = path.suffix.lstrip(".")

    if action == "run":
        if ext == "py":
            return _run_python_with_confirmation(path)
        return f"Direct execution not supported for .{ext} files."

    if action == "info":
        lines = content.count("\n")
        words = len(content.split())
        return f"Code file: {lines} lines, {words} words, {_file_size_str(path)}"

    prompt_map = {
        "explain":   f"Explain this {ext} code clearly:\n\n```{ext}\n{content[:30000]}\n```",
        "review":    f"Review this {ext} code for bugs, issues, and improvements:\n\n```{ext}\n{content[:30000]}\n```",
        "fix":       f"Fix any bugs in this {ext} code and return the corrected version:\n\n```{ext}\n{content[:30000]}\n```",
        "optimize":  f"Optimize this {ext} code for performance and readability:\n\n```{ext}\n{content[:30000]}\n```",
        "document":  f"Add proper documentation/comments to this {ext} code:\n\n```{ext}\n{content[:30000]}\n```",
        "summarize": f"Summarize what this {ext} code does:\n\n```{ext}\n{content[:30000]}\n```",
        "test":      f"Write unit tests for this {ext} code:\n\n```{ext}\n{content[:30000]}\n```",
    }

    instruction = params.get("instruction", "")
    if action not in prompt_map:
        prompt = f"{action}\n\n```{ext}\n{content[:30000]}\n```"
        if instruction:
            prompt = f"{instruction}\n\n```{ext}\n{content[:30000]}\n```"
    else:
        prompt = prompt_map[action]

    try:
        model    = _gemini_client()
        response = model.generate_content(prompt)
        result   = response.text.strip()

        if action in ("fix", "optimize", "document") and params.get("save", True):
            out = _output_path(path, action)
            code_match = re.search(r"```(?:\w+)?\n(.*?)```", result, re.DOTALL)
            code_to_save = code_match.group(1) if code_match else result
            _secure_write_new_text(out, code_to_save)
            _verify_output_artifact(out)
            return f"{result[:400]}...\n\nSaved: {out.name}"
        return result
    except Exception as e:
        _cleanup_generated_artifact(locals().get("out"))
        return f"AI processing failed: {e}"

def _process_audio(path: Path, action: str, params: dict, speak=None) -> str:
    action = action or "transcribe"

    if action == "info":
        try:
            from pydub import AudioSegment
            audio    = AudioSegment.from_file(path)
            duration = len(audio) / 1000
            mins, secs = divmod(int(duration), 60)
            return (f"Audio: {mins}m {secs}s, "
                    f"{audio.channels} ch, "
                    f"{audio.frame_rate}Hz, "
                    f"{_file_size_str(path)}")
        except ImportError:
            return f"Audio file: {_file_size_str(path)} (install pydub for more info)"
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"Info failed: {e}"

    if action == "transcribe":
        try:
            model   = _gemini_client()
            content = path.read_bytes()
            mime    = {
                "mp3": "audio/mp3", "wav": "audio/wav",
                "ogg": "audio/ogg", "m4a": "audio/mp4",
                "aac": "audio/aac", "flac": "audio/flac",
            }.get(path.suffix.lstrip(".").lower(), "audio/mpeg")
            response = model.generate_content([
                "Transcribe all speech in this audio file accurately.",
                {"mime_type": mime, "data": content}
            ])
            result = response.text.strip()
            if params.get("save", True):
                out = _output_path(path, "transcript", ".txt")
                _secure_write_new_text(out, result)
                _verify_output_artifact(out)
                return f"Transcription saved: {out.name}\n\nPreview: {result[:300]}"
            return result
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"Transcription failed: {e}"

    if action == "convert":
        fmt = params.get("format", "mp3").lstrip(".")
        try:
            from pydub import AudioSegment
            audio = AudioSegment.from_file(path)
            out   = _output_path(path, "converted", f".{fmt}")
            audio.export(out, format=fmt)
            _verify_output_artifact(out)
            return f"Converted to {fmt.upper()}. Saved: {out.name}"
        except ImportError:
            return "pydub not installed. Run: pip install pydub"
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"Convert failed: {e}"

    if action == "trim":
        start = float(params.get("start", 0))
        end   = float(params.get("end",   0))
        try:
            from pydub import AudioSegment
            audio   = AudioSegment.from_file(path)
            end_ms  = int(end * 1000)   if end   else len(audio)
            trimmed = audio[int(start * 1000):end_ms]
            out     = _output_path(path, f"trim_{int(start)}s_{int(end)}s")
            trimmed.export(out, format=path.suffix.lstrip("."))
            _verify_output_artifact(out)
            return f"Trimmed audio ({int(start)}s–{int(end)}s). Saved: {out.name}"
        except ImportError:
            return "pydub not installed."
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"Trim failed: {e}"

    return f"Unknown audio action: '{action}'. Try: transcribe, info, convert, trim"

def _process_video(path: Path, action: str, params: dict, speak=None) -> str:
    action = action or "info"


    def _ffmpeg_available() -> bool:
        try:
            result = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=3)
            return result.returncode == 0
        except Exception:
            return False

    if action == "info":
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_format", "-show_streams", str(path)],
                capture_output=True, text=True, timeout=10
            )
            data     = json.loads(result.stdout)
            fmt      = data.get("format", {})
            duration = float(fmt.get("duration", 0))
            mins, secs = divmod(int(duration), 60)
            size     = _file_size_str(path)
            streams  = data.get("streams", [])
            video_s  = next((s for s in streams if s["codec_type"] == "video"), {})
            w        = video_s.get("width", "?")
            h        = video_s.get("height", "?")
            fps      = video_s.get("r_frame_rate", "?")
            return f"Video: {mins}m {secs}s, {w}x{h}, {fps} fps, {size}"
        except Exception:
            return f"Video file: {_file_size_str(path)}"

    if action == "extract_audio":
        if not _ffmpeg_available():
            return "ffmpeg not found. Install ffmpeg to extract audio."
        out = _output_path(path, "audio", ".mp3")
        try:
            result = subprocess.run(
                ["ffmpeg", "-i", str(path), "-q:a", "0", "-map", "a", str(out), "-y"],
                capture_output=True, timeout=300
            )
            if result.returncode != 0:
                _cleanup_generated_artifact(locals().get("out"))
                return f"Extract audio failed (ffmpeg exit {result.returncode})."
            _verify_output_artifact(out)
            return f"Audio extracted. Saved: {out.name}"
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"Extract audio failed: {e}"

    if action == "trim":
        start = params.get("start", "00:00:00")
        end   = params.get("end",   "")
        if not _ffmpeg_available():
            return "ffmpeg not found."
        out = _output_path(path, f"trim", path.suffix)
        try:
            cmd = ["ffmpeg", "-i", str(path), "-ss", str(start)]
            if end:
                cmd += ["-to", str(end)]
            cmd += ["-c", "copy", str(out), "-y"]
            result = subprocess.run(cmd, capture_output=True, timeout=600)
            if result.returncode != 0:
                _cleanup_generated_artifact(locals().get("out"))
                return f"Trim failed (ffmpeg exit {result.returncode})."
            _verify_output_artifact(out)
            return f"Trimmed video saved: {out.name}"
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"Trim failed: {e}"

    if action == "extract_frame":
        timestamp = params.get("timestamp", "00:00:01")
        if not _ffmpeg_available():
            return "ffmpeg not found."
        out = _output_path(path, f"frame_{timestamp.replace(':', '')}", ".jpg")
        try:
            result = subprocess.run(
                ["ffmpeg", "-i", str(path), "-ss", timestamp,
                 "-vframes", "1", str(out), "-y"],
                capture_output=True, timeout=30
            )
            if result.returncode != 0:
                _cleanup_generated_artifact(locals().get("out"))
                return f"Extract frame failed (ffmpeg exit {result.returncode})."
            _verify_output_artifact(out)
            return f"Frame extracted at {timestamp}. Saved: {out.name}"
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"Extract frame failed: {e}"

    if action == "compress":
        crf = int(params.get("quality", 28))  
        if not _ffmpeg_available():
            return "ffmpeg not found."
        out = _output_path(path, f"compressed_crf{crf}", ".mp4")
        try:
            result = subprocess.run(
                ["ffmpeg", "-i", str(path),
                 "-c:v", "libx264", "-crf", str(crf),
                 "-preset", "medium", "-c:a", "copy",
                 str(out), "-y"],
                capture_output=True, timeout=1800
            )
            if result.returncode != 0:
                _cleanup_generated_artifact(locals().get("out"))
                return f"Compress failed (ffmpeg exit {result.returncode})."
            before = _file_size_str(path)
            after  = _file_size_str(out)
            _verify_output_artifact(out)
            return f"Compressed: {before} → {after}. Saved: {out.name}"
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"Compress failed: {e}"

    if action == "transcribe":
        if not _ffmpeg_available():
            return "ffmpeg not found. Needed for video transcription."
        tmp_fd, tmp_name = tempfile.mkstemp(suffix=".mp3")
        os.close(tmp_fd)
        tmp_audio = Path(tmp_name)
        try:
            result = subprocess.run(
                ["ffmpeg", "-i", str(path), "-q:a", "0", "-map", "a",
                 str(tmp_audio), "-y"],
                capture_output=True, timeout=300
            )
            if result.returncode != 0:
                _cleanup_generated_artifact(locals().get("out"))
                return f"Video transcription failed (ffmpeg exit {result.returncode})."
            result = _process_audio(tmp_audio, "transcribe", params, speak)
            return result
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"Video transcription failed: {e}"
        finally:
            if tmp_audio.exists():
                tmp_audio.unlink()

    if action == "convert":
        fmt = params.get("format", "mp4").lstrip(".")
        if not _ffmpeg_available():
            return "ffmpeg not found."
        out = _output_path(path, "converted", f".{fmt}")
        try:
            result = subprocess.run(
                ["ffmpeg", "-i", str(path), str(out), "-y"],
                capture_output=True, timeout=1800
            )
            if result.returncode != 0:
                _cleanup_generated_artifact(locals().get("out"))
                return f"Convert failed (ffmpeg exit {result.returncode})."
            _verify_output_artifact(out)
            return f"Converted to {fmt.upper()}. Saved: {out.name}"
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"Convert failed: {e}"

    return f"Unknown video action: '{action}'. Try: info, trim, extract_audio, extract_frame, compress, transcribe, convert"

def _safe_archive_destination(value: str | Path, fallback: Path) -> Path:
    raw = Path(value).expanduser() if str(value).strip() else fallback
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    home = Path.home().resolve()
    try:
        resolved = raw.resolve(strict=False)
        resolved.relative_to(home)
    except (OSError, ValueError) as exc:
        raise ValueError("Archive extraction is limited to destinations inside the user's home directory.") from exc
    current = Path(raw.anchor) if raw.anchor else Path(".")
    parts = raw.parts[1:] if raw.anchor else raw.parts
    for part in parts:
        current = current / part
        try:
            if current.is_symlink():
                raise ValueError("Archive extraction destinations may not contain symlinked path components.")
        except OSError as exc:
            raise ValueError("Unable to safely validate the archive destination path.") from exc
    return resolved


def _safe_archive_target(root: Path, member_name: str) -> Path:
    normalized = str(member_name or "").replace("\\", "/")
    if not normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError("Archive contains an absolute member path.")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if ".." in parts:
        raise ValueError("Archive contains a path-traversal member.")
    target = (root / Path(*parts)).resolve()
    target.relative_to(root.resolve())
    return target


def _safe_extract_archive(path: Path, dest: Path) -> None:
    import os
    import tarfile
    import zipfile

    MAX_ARCHIVE_MEMBERS = 10_000
    MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
    MAX_ARCHIVE_COMPRESSION_RATIO = 1000

    dest = Path(dest).absolute()
    home = Path.home().resolve()
    try:
        dest.relative_to(home)
    except ValueError as exc:
        raise ValueError("Archive extraction destination must stay inside the user's home directory.") from exc
    if dest.exists() and (dest.is_symlink() or not dest.is_dir()):
        raise ValueError("Archive extraction destination must be a real directory.")

    created_files: list[tuple[Path, tuple[int, int] | None, tuple[int, int, int] | None]] = []
    created_dirs: list[Path] = []

    def _fingerprint(target: Path) -> tuple[int, int] | None:
        try:
            st = target.stat(follow_symlinks=False)
            return int(getattr(st, "st_dev", 0)), int(getattr(st, "st_ino", 0))
        except OSError:
            return None

    def _open_directory(root: Path, target: Path):
        """Open/create a directory chain without following symlinks on POSIX."""
        root = Path(root).absolute()
        target = Path(target).absolute()
        try:
            relative = target.relative_to(root)
        except ValueError as exc:
            raise ValueError("Archive extraction path escaped its safe root.") from exc
        if root.is_symlink() or not root.is_dir():
            raise ValueError("Archive extraction root must be a real directory.")
        if os.name != "nt" and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"):
            flags_dir = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            fd = os.open(str(root), flags_dir)
            current = root
            try:
                for part in relative.parts:
                    current = current / part
                    try:
                        os.mkdir(part, mode=0o755, dir_fd=fd)
                        created_dirs.append(current)
                    except FileExistsError:
                        pass
                    next_fd = os.open(part, flags_dir, dir_fd=fd)
                    os.close(fd)
                    fd = next_fd
                return fd
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise

        current = root
        for part in relative.parts:
            current = current / part
            if os.name == "nt" and _WINFS is not None:
                try:
                    created = _WINFS.ensure_directory(current)
                except OSError as exc:
                    raise ValueError("Archive extraction path contains an unsafe Windows directory component.") from exc
                if created:
                    created_dirs.append(current)
                continue
            is_junction = getattr(current, "is_junction", None)
            if current.is_symlink() or (is_junction is not None and is_junction()):
                raise ValueError("Archive extraction path contains a link/reparse component.")
            if not current.exists():
                current.mkdir()
                created_dirs.append(current)
            elif not current.is_dir():
                raise ValueError("Archive extraction path contains a non-directory component.")
        return None

    if not dest.exists():
        _open_directory(home, dest)
    if dest.is_symlink() or not dest.is_dir():
        raise ValueError("Archive extraction destination must be a real directory.")

    def _open_output(root: Path, target: Path):
        """Open an archive output exclusively beneath a race-resistant directory chain."""
        relative = target.relative_to(root)
        parts = relative.parts
        if not parts:
            raise ValueError("Archive member resolved to its extraction root.")
        parent_fd = None
        if os.name != "nt" and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"):
            parent_fd = _open_directory(root, target.parent)
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                fd = os.open(parts[-1], flags, 0o600, dir_fd=parent_fd)
                handle = os.fdopen(fd, "wb")
                created_files.append((target, _fingerprint(target), None))
                return handle
            finally:
                if parent_fd is not None:
                    os.close(parent_fd)

        _open_directory(root, target.parent)
        if target.is_symlink():
            raise ValueError("Archive extraction target became a symlink.")
        if os.name == "nt" and _WINFS is not None:
            fd, _final, info = _WINFS.open_safe_file(
                target,
                write=True,
                create_new=True,
                exclusive=True,
            )
            identity = (
                int(info.dwVolumeSerialNumber),
                int(info.nFileIndexHigh),
                int(info.nFileIndexLow),
            )
            created_files.append((target, _fingerprint(target), identity))
            return os.fdopen(fd, "wb", closefd=True)
        handle = target.open("xb")
        created_files.append((target, _fingerprint(target), None))
        return handle

    def preflight(members):
        total_size = 0
        seen_types: dict[Path, bool] = {}
        planned: list[tuple[object, Path, bool]] = []
        for member in members:
            size = max(0, int(getattr(member, "file_size", getattr(member, "size", 0))))
            total_size += size
            if total_size > MAX_ARCHIVE_BYTES:
                raise ValueError("Archive expands beyond the 1 GiB extraction limit.")
            name = getattr(member, "filename", getattr(member, "name", ""))
            target = _safe_archive_target(dest, name)
            if target != dest and target.exists():
                raise ValueError(f"Archive would overwrite an existing path: {target.name}")
            is_dir = member.is_dir() if isinstance(member, zipfile.ZipInfo) else member.isdir()
            previous = seen_types.get(target)
            if previous is not None:
                if previous != is_dir or not is_dir:
                    raise ValueError(f"Archive contains duplicate output path: {target.name}")
            else:
                seen_types[target] = is_dir

            planned.append((member, target, is_dir))

            if isinstance(member, zipfile.ZipInfo):
                mode = (member.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise ValueError("Archive symlink members are not allowed.")
                compressed = max(0, int(member.compress_size))
                if not is_dir and compressed > 0 and size > compressed * MAX_ARCHIVE_COMPRESSION_RATIO:
                    raise ValueError("Archive member compression ratio exceeds the safety limit.")
                if not is_dir and compressed == 0 and size > 0:
                    raise ValueError("Archive member has an unsafe zero-size compressed representation.")
            else:
                if member.issym() or member.islnk():
                    raise ValueError("Archive link members are not allowed.")
                if not member.isdir() and not member.isfile():
                    raise ValueError("Archive special-file members are not allowed.")
        for _, target, _ in planned:
            if any(parent in seen_types and seen_types[parent] is False for parent in target.parents if parent != dest):
                raise ValueError(f"Archive contains a file/directory path conflict under {target.name}.")
        return [(member, target, is_dir) for member, target, is_dir in planned]

    total_written = 0

    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_ARCHIVE_MEMBERS:
                    raise ValueError("Archive contains too many members.")
                planned = preflight(infos)
                for info, target, is_dir in planned:
                    if is_dir:
                        _open_directory(dest, target)
                        continue
                    with archive.open(info, "r") as source, _open_output(dest, target) as output:
                        written = 0
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > int(info.file_size) or total_written + written > MAX_ARCHIVE_BYTES:
                                raise ValueError("Archive emitted more data than its declared or allowed extraction size.")
                            output.write(chunk)
                        total_written += written
                        if written != int(info.file_size):
                            raise ValueError("Archive member size did not match its declared size.")
            return

        with tarfile.open(path) as archive:
            members = archive.getmembers()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ValueError("Archive contains too many members.")
            planned = preflight(members)
            archive_bytes = max(1, int(path.stat().st_size))
            if total_size := sum(
                max(0, int(getattr(member, "file_size", getattr(member, "size", 0))))
                for member, _target, is_dir in planned
                if not is_dir
            ):
                if total_size > archive_bytes * MAX_ARCHIVE_COMPRESSION_RATIO:
                    raise ValueError("Archive compression ratio exceeds the safety limit.")
            for member, target, is_dir in planned:
                if is_dir:
                    _open_directory(dest, target)
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("Archive member could not be read safely.")
                with source, _open_output(dest, target) as output:
                    written = 0
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > int(member.file_size) or total_written + written > MAX_ARCHIVE_BYTES:
                            raise ValueError("Archive emitted more data than its declared or allowed extraction size.")
                        output.write(chunk)
                    total_written += written
                    if written != int(member.file_size):
                        raise ValueError("Archive member size did not match its declared size.")
    except Exception:
        for target, fingerprint, identity in reversed(created_files):
            try:
                if fingerprint is not None and _fingerprint(target) == fingerprint:
                    if os.name == "nt" and _WINFS is not None and identity is not None:
                        _WINFS.unlink(target, expected_identity=identity)
                    else:
                        target.unlink(missing_ok=True)
            except OSError:
                pass
        for directory in reversed(created_dirs):
            try:
                if directory.exists() and directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
                    directory.rmdir()
            except OSError:
                pass
        raise

def _process_archive(path: Path, action: str, params: dict, speak=None) -> str:
    action = action or "list"

    if action == "list":
        try:
            import zipfile, tarfile
            ext = path.suffix.lower()
            if ext == ".zip":
                with zipfile.ZipFile(path) as z:
                    names = z.namelist()
            elif ext in (".tar", ".gz", ".bz2", ".xz"):
                with tarfile.open(path) as t:
                    names = t.getnames()
            else:
                return f"Unsupported archive format: {ext}"
            preview = "\n".join(names[:30])
            suffix  = f"\n... and {len(names)-30} more" if len(names) > 30 else ""
            return f"Archive contains {len(names)} files:\n{preview}{suffix}"
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"List failed: {e}"

    if action == "extract":
        try:
            dest = _safe_archive_destination(
                params.get("destination", str(path.parent / path.stem)),
                path.parent / path.stem,
            )
            dest.mkdir(parents=True, exist_ok=True)
            if dest.is_symlink():
                raise ValueError("Archive extraction destination may not be a symlink.")
            _safe_extract_archive(path, dest)
            return f"Extracted to: {dest}"
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"Extract failed: {e}"

    return f"Unknown archive action: '{action}'. Try: list, extract"

def _process_pptx(path: Path, action: str, params: dict, speak=None) -> str:
    action = action or "summarize"

    def _read_pptx_text() -> str:
        try:
            from pptx import Presentation
            prs  = Presentation(path)
            if len(prs.slides) > MAX_PRESENTATION_SLIDES:
                raise ValueError(f"Presentation exceeds the {MAX_PRESENTATION_SLIDES}-slide processing limit.")
            text = []
            for i, slide in enumerate(prs.slides, 1):
                slide_text = f"\n--- Slide {i} ---\n"
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text.strip():
                        slide_text += shape.text.strip() + "\n"
                text.append(slide_text)
            return "\n".join(text)
        except ImportError:
            return "python-pptx not installed."

    if action in ("summarize", "extract_text", "analyze"):
        text = _read_pptx_text()
        if action == "extract_text":
            out = _output_path(path, "text", ".txt")
            _secure_write_new_text(out, text)
            _verify_output_artifact(out)
            return f"Text extracted. Saved: {out.name}"
        try:
            model    = _gemini_client()
            prompt   = f"{'Summarize' if action == 'summarize' else 'Analyze'} this presentation:\n{text[:30000]}"
            response = model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"AI processing failed: {e}"

    return f"Unknown PPTX action: '{action}'. Try: summarize, extract_text, analyze"

def _resolve_input_path(value: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("No file path provided.")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    resolved = path.resolve(strict=False)
    home = Path.home().resolve()
    try:
        resolved.relative_to(home)
    except ValueError as exc:
        raise ValueError("File processing is limited to paths inside the user's home directory.") from exc
    if path.is_symlink() and not resolved.exists():
        raise FileNotFoundError(f"File not found: {value}")
    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {value}")
    if not resolved.is_file():
        raise ValueError(f"Path is not a file: {value}")
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise RuntimeError(f"Unable to inspect input file safely: {value}") from exc
    if size > MAX_INPUT_BYTES:
        raise ValueError(f"Input file exceeds the {MAX_INPUT_BYTES // (1024 * 1024)} MiB processing limit.")
    return resolved


def _run_python_with_confirmation(path: Path) -> str:
    from core.confirm import request

    def _execute() -> str:
        try:
            result = subprocess.run(
                ["python", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(path.parent),
                check=False,
            )
            output = (result.stdout or result.stderr or "").strip()
            if result.returncode != 0:
                raise RuntimeError(
                    f"Python execution failed (exit {result.returncode}): {output[:1200]}"
                )
            return f"Python execution completed. Output:\n{output[:2000]}" if output else "Python execution completed with no output."
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Python execution timed out after 30 seconds.") from exc

    return request(
        key=f"file-execute:{path}",
        title="Run Python file",
        detail=f"Run the Python file '{path.name}' from '{path.parent}'?",
        run=_execute,
    )


def file_processor(parameters: dict, player=None, speak=None) -> str:
    file_path_str = parameters.get("file_path", "").strip()
    if not file_path_str:
        return "No file path provided."

    try:
        path = _resolve_input_path(file_path_str)
    except (ValueError, FileNotFoundError) as exc:
        return f"File processing denied: {exc}"

    file_type   = _detect_type(path)
    action      = (parameters.get("action") or "").lower().strip()
    instruction = parameters.get("instruction", "")
    params      = {**parameters, "instruction": instruction}

    log_msg = f"[FileProcessor] {file_type.upper()} | {path.name} | action={action or 'auto'}"
    print(log_msg)
    if player:
        player.write_log(log_msg)

    if file_type == "unknown":
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")[:10000]
            model   = _gemini_client()
            prompt  = f"File: {path.name}\nContent preview:\n{content}\n\nTask: {action or instruction or 'Describe what this file contains and what can be done with it.'}"
            response = model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            _cleanup_generated_artifact(locals().get("out"))
            return f"Unknown file type ({path.suffix}). Could not process: {e}"

    dispatch = {
        "image":   _process_image,
        "pdf":     _process_pdf,
        "docx":    lambda p, a, pm, s: _process_text_doc(p, "docx", a, pm, s),
        "text":    lambda p, a, pm, s: _process_text_doc(p, "text", a, pm, s),
        "csv":     lambda p, a, pm, s: _process_data(p, "csv",   a, pm, s),
        "excel":   lambda p, a, pm, s: _process_data(p, "excel", a, pm, s),
        "json":    _process_json,
        "xml":     lambda p, a, pm, s: _process_json(p, a, pm, s),  
        "code":    _process_code,
        "audio":   _process_audio,
        "video":   _process_video,
        "archive": _process_archive,
        "pptx":    _process_pptx,
    }

    handler = dispatch.get(file_type)
    if not handler:
        return f"Unsupported file type: {file_type}"

    try:
        result = handler(path, action, params, speak)
        return result or "Done."
    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"Processing failed: {e}"