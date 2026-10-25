"""Bounded extraction in a disposable worker; never execute source content."""

import base64
import io
import json
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .source import source_file

TEXT = {
    ".txt",
    ".md",
    ".rst",
    ".csv",
    ".tsv",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".html",
    ".htm",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".css",
    ".sql",
    ".sh",
    ".ini",
    ".cfg",
    ".log",
    ".tex",
    ".ipynb",
    ".toml",
    ".eml",
    ".vcf",
    ".ics",
}
IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".tif", ".heic"}
MAX_INPUT = 2 * 1024 * 1024


def extract(stream, ext, size, image=False):
    result = {"status": "metadata", "text": "", "bytes_read": 0}
    if ext in TEXT:
        data = stream.read(16000)
        if b"\0" in data[:1000] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
            result["text"] = "Binary content with a text extension; not interpreted."
            return result
        encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        result.update(
            status="excerpt", text=data.decode(encoding, errors="replace")[:6000], bytes_read=len(data)
        )
    elif ext in {".zip", ".docx", ".xlsx", ".pptx"}:
        if size > MAX_INPUT:
            return {**result, "text": "Archive/Office file larger than 2 MiB; metadata only."}
        data = stream.read(MAX_INPUT)
        result["bytes_read"] = len(data)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if ext == ".zip":
                result.update(
                    status="excerpt",
                    text="Archive listing (not extracted):\n"
                    + "\n".join(i.filename[:250] for i in entries[:80]),
                )
            else:
                texts = []
                for entry in entries:
                    if (
                        (ext == ".docx" and entry.filename == "word/document.xml")
                        or (ext == ".xlsx" and entry.filename in {"xl/sharedStrings.xml", "xl/workbook.xml"})
                        or (
                            ext == ".pptx"
                            and entry.filename.startswith("ppt/slides/slide")
                            and entry.filename.endswith(".xml")
                        )
                    ):
                        if entry.file_size > MAX_INPUT or entry.file_size > max(1, entry.compress_size) * 150:
                            continue
                        payload = archive.read(entry)
                        if b"<!DOCTYPE" in payload or b"<!ENTITY" in payload:
                            continue
                        xml = ElementTree.fromstring(payload)
                        texts.append(" ".join(t for t in xml.itertext())[:6000])
                        if len(texts) >= 3:
                            break
                result.update(
                    status="excerpt" if texts else "metadata",
                    text="\n".join(texts)[:6000] or "No safely readable Office excerpts.",
                )
    elif ext == ".pdf":
        if size > MAX_INPUT:
            return {**result, "text": "PDF larger than 2 MiB; metadata only."}
        try:
            from pypdf import PdfReader
        except ImportError:
            return {**result, "text": "PDF reader unavailable; install the documents extra."}
        data = stream.read(MAX_INPUT)
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            return {**result, "text": "Encrypted PDF; contents not examined.", "bytes_read": len(data)}
        text = "\n".join((page.extract_text() or "")[:2500] for page in reader.pages[:3])
        result.update(
            status="excerpt" if text.strip() else "metadata",
            text=text[:6000] or "No extractable text; possibly a scanned image.",
            bytes_read=len(data),
        )
    elif ext in IMAGES:
        if size > MAX_INPUT:
            return {**result, "text": "Image larger than 2 MiB; metadata only."}
        try:
            from PIL import Image
        except ImportError:
            return {**result, "text": "Image reader unavailable; install the documents extra."}
        Image.MAX_IMAGE_PIXELS = 25_000_000
        data = stream.read(MAX_INPUT)
        with Image.open(io.BytesIO(data)) as img:
            result.update(
                text=f"Image: {img.width} × {img.height} Pixel ({img.format}).", bytes_read=len(data)
            )
            if image:
                img.thumbnail((512, 512))
                buf = io.BytesIO()
                img.convert("RGB").save(buf, "JPEG", quality=65)
                result["image"] = base64.b64encode(buf.getvalue()).decode("ascii")
                result["status"] = "image"
    else:
        result["text"] = "File type cataloged from metadata only; contents not read."
    return result


def main():
    # A malformed PDF or compressed XML cannot consume unlimited process resources.
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024**2, 768 * 1024**2))
    except (ImportError, ValueError, OSError):
        pass
    args = json.loads(sys.stdin.read(20000))
    try:
        with source_file(args["root"], args["rel"], args["device"]) as (stream, info):
            if info.st_size != args["size"] or abs(info.st_mtime - args["mtime"]) > 0.001:
                raise ValueError("File changed since inventory; sample skipped.")
            result = extract(stream, Path(args["rel"]).suffix.lower(), info.st_size, args.get("image", False))
    except Exception as exc:
        result = {"status": "error", "text": str(exc)[:300], "bytes_read": 0}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
