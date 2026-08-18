from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path, PureWindowsPath
from typing import Iterable

import requests
from pypdf import PdfReader

from . import __version__

MINERU_BATCH_URL = "https://mineru.net/api/v4/file-urls/batch"
MINERU_RESULT_URL = "https://mineru.net/api/v4/extract-results/batch/{batch_id}"
MAX_ZIP_ENTRIES = 20_000
MAX_ZIP_ENTRY_BYTES = 256 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 1024 * 1024 * 1024


CHINESE_NUM = "零〇一二两三四五六七八九十百千"


@dataclass
class Chapter:
    number: str
    title: str
    start: int
    end: int
    content: str
    line_start: int = 0
    line_end: int = 0
    image_count: int = 0
    missing_images: list[str] | None = None
    node_id: str = ""
    kind: str = ""
    logical_level: int = 1
    confidence: float = 0.0
    needs_review: bool = False
    review_reasons: list[str] | None = None
    page_start: int | None = None
    page_end: int | None = None
    output_folder: str = ""


@dataclass
class StructureNode:
    """A TOC or body heading normalized independently from Markdown depth."""

    node_id: str
    number: str
    title: str
    kind: str
    logical_level: int
    start: int
    line: int
    page: int | None = None
    toc_page: int | None = None
    source: str = "body"


@dataclass
class SplitPlan:
    chapters: list[Chapter]
    method: str
    needs_review: bool
    review_reasons: list[str]
    toc_nodes: list[StructureNode]
    body_nodes: list[StructureNode]
    unmatched_toc_nodes: list[StructureNode]


def log(message: str) -> None:
    now = dt.datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def safe_name(value: str, fallback: str = "未命名") -> str:
    value = re.sub(r'[\\/:*?"<>|]', "_", str(value or ""))
    value = re.sub(r"\s+", " ", value).strip()
    return value or fallback


def sha256_file(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.digest()


def quote_yaml(value: str) -> str:
    return '"' + str(value or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def normalize_md(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def looks_like_token(value: str) -> bool:
    return value.startswith("eyJ") or value.startswith("sk-") or ("." in value and len(value) > 32)


def resolve_token(value: str) -> str:
    if not value:
        raise RuntimeError("MinerU official API token environment variable is empty.")
    if looks_like_token(value):
        return value
    env_value = os.environ.get(value, "").strip()
    if env_value:
        return env_value
    for env_file in (Path(".env.local"), Path(".env")):
        if not env_file.exists():
            continue
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            if raw.startswith("export "):
                raw = raw[7:].lstrip()
            if "=" not in raw:
                continue
            key, val = raw.split("=", 1)
            if key.strip() == value:
                return val.strip().strip('"').strip("'")
    raise RuntimeError(f"MinerU official API token not found: {value}")


def page_ranges(page_count: int, size: int) -> list[tuple[int, int]]:
    return [(start, min(start + size - 1, page_count)) for start in range(1, page_count + 1, size)]


def semantic_page_ranges(
    page_count: int,
    max_size: int,
    outline_nodes: list[StructureNode] | None,
) -> tuple[list[tuple[int, int]], dict]:
    """Balance MinerU uploads at PDF bookmark boundaries instead of every N pages.

    The PDF outline is read before upload.  Every bookmark start is a legal
    cut point, so chapters are never split merely because a page counter rolls
    over.  Dynamic programming chooses the fixed number of API-safe chunks
    with the smallest total deviation from an even target size.
    """
    fallback = page_ranges(page_count, max_size)
    if not outline_nodes:
        return fallback, {"method": "fixed-page-fallback", "reason": "No usable PDF bookmarks."}

    semantic_kinds = {"part", "chapter", "section"}
    starts = sorted(
        {1, page_count + 1}
        | {
            node.page
            for node in outline_nodes
            if node.page and node.kind in semantic_kinds and 1 <= node.page <= page_count
        }
    )
    chunk_count = max(1, math.ceil(page_count / max_size))
    target = page_count / chunk_count
    costs: dict[tuple[int, int], tuple[float, list[int]]] = {(0, 0): (0.0, [1])}
    for used in range(chunk_count):
        for (count, boundary_index), (cost, path) in list(costs.items()):
            if count != used:
                continue
            start = starts[boundary_index]
            for next_index in range(boundary_index + 1, len(starts)):
                end_exclusive = starts[next_index]
                length = end_exclusive - start
                if length > max_size:
                    break
                next_cost = cost + (length - target) ** 2
                key = (used + 1, next_index)
                candidate = (next_cost, path + [end_exclusive])
                if key not in costs or candidate[0] < costs[key][0]:
                    costs[key] = candidate
    final = costs.get((chunk_count, len(starts) - 1))
    if not final:
        raise RuntimeError(
            "Bookmark boundaries cannot satisfy the API page limit. "
            "Add finer section bookmarks or choose a smaller semantic unit; refusing fixed-page fallback."
        )
    boundaries = final[1]
    ranges = [(boundaries[index], boundaries[index + 1] - 1) for index in range(len(boundaries) - 1)]
    range_nodes = []
    for start, end in ranges:
        nodes = [node for node in outline_nodes if node.page == start and node.kind in semantic_kinds]
        range_nodes.append(
            {
                "startPage": start,
                "endPage": end,
                "startNodes": [
                    {"kind": node.kind, "number": node.number, "title": node.title}
                    for node in nodes
                ],
            }
        )
    return ranges, {
        "method": "pdf-bookmark-balanced",
        "targetPages": target,
        "maxPages": max_size,
        "bookmarkCount": len(outline_nodes),
        "boundaries": boundaries,
        "rangeNodes": range_nodes,
    }


def data_id(seed: str) -> str:
    h = 2166136261
    for ch in seed:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return f"mineru-textbook-{base36(h)}-{base36(int(time.time() * 1000))}"


def base36(num: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if num == 0:
        return "0"
    out = ""
    while num:
        num, rem = divmod(num, 36)
        out = alphabet[rem] + out
    return out


def http_json(req: urllib.request.Request, label: str) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{label} ({exc.code}): {detail}") from exc
    if not text.strip():
        raise RuntimeError(f"{label}: empty response")
    return json.loads(text)


def request_upload_url(
    token: str,
    pdf_path: Path,
    pdf_name: str,
    page_range: tuple[int, int] | None,
    settings: dict,
) -> tuple[str, str]:
    file_spec = {
        "name": (
            f"{pdf_path.stem}-{page_range[0]}-{page_range[1]}.pdf"
            if page_range
            else pdf_name
        ),
        "data_id": data_id(f"{pdf_path}-{page_range or 'full'}"),
        "is_ocr": settings["method"] == "ocr",
    }
    if page_range:
        file_spec["page_ranges"] = f"{page_range[0]}-{page_range[1]}"
    payload = {
        "files": [file_spec],
        "model_version": settings["modelVersion"],
        "enable_formula": settings["formula"],
        "enable_table": settings["table"],
    }
    req = urllib.request.Request(
        MINERU_BATCH_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
        },
    )
    body = http_json(req, "申请 MinerU 上传 URL 失败")
    batch_id = ((body.get("data") or {}).get("batch_id")) or ""
    file_urls = ((body.get("data") or {}).get("file_urls")) or []
    if body.get("code") != 0 or not batch_id or not file_urls:
        raise RuntimeError(
            "申请 MinerU 上传 URL 失败: "
            + str(body.get("msg") or "response missing batch_id/file_urls")
        )
    return batch_id, str(file_urls[0])


def upload_pdf(upload_url: str, pdf_path: Path) -> None:
    with pdf_path.open("rb") as handle:
        resp = requests.put(upload_url, data=handle, timeout=300)
    if not resp.ok:
        raise RuntimeError(f"上传 PDF 到 MinerU 存储失败 ({resp.status_code}): {resp.text}")


def poll_result(token: str, batch_id: str, timeout_seconds: int = 1800) -> dict:
    url = MINERU_RESULT_URL.format(batch_id=batch_id)
    deadline = time.time() + timeout_seconds
    last_state = ""
    while time.time() < deadline:
        req = urllib.request.Request(
            url,
            method="GET",
            headers={"Authorization": f"Bearer {token}", "Accept": "*/*"},
        )
        body = http_json(req, "查询 MinerU 解析状态失败")
        if body.get("code") != 0:
            raise RuntimeError("查询 MinerU 解析状态失败: " + str(body.get("msg") or "unknown"))
        results = ((body.get("data") or {}).get("extract_result")) or []
        if results:
            item = results[0]
            state = str(item.get("state") or "")
            if state != last_state:
                log(f"MinerU 状态: {state or 'unknown'}")
                last_state = state
            if state == "failed":
                raise RuntimeError("MinerU 解析失败: " + str(item.get("err_msg") or "unknown"))
            if state == "done" or item.get("full_zip_url"):
                if not item.get("full_zip_url"):
                    raise RuntimeError("MinerU 结果缺少 full_zip_url")
                return item
        time.sleep(5)
    raise RuntimeError(f"MinerU 解析超时: {batch_id}")


def download_file(url: str, direct: bool = False) -> bytes:
    """Download a MinerU result, respecting system proxy settings by default.

    Some environments need a direct CDN connection; callers can opt into that
    behavior explicitly instead of silently bypassing the user's proxy.
    """
    proxies = {"http": None, "https": None} if direct else None
    last_error: Exception | None = None
    for attempt in range(1, 6):
        # 首选 requests（直连）
        try:
            resp = requests.get(url, timeout=300, proxies=proxies)
            if resp.ok:
                return resp.content
            last_error = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        except requests.RequestException as exc:
            last_error = exc
        # 兜底：curl.exe 直连（--noproxy 绕开代理）
        try:
            import subprocess
            import tempfile
            curl = shutil.which("curl.exe") or shutil.which("curl")
            if curl:
                fd, tmp = tempfile.mkstemp(suffix=".zip")
                os.close(fd)
                try:
                    curl_args = [curl, "-L", "--retry", "3", "--retry-delay", "2", "-sS"]
                    if direct:
                        curl_args.extend(["--noproxy", "*"])
                    curl_args.extend(["-o", tmp, url])
                    r = subprocess.run(
                        curl_args,
                        capture_output=True, timeout=600,
                    )
                    if r.returncode == 0 and os.path.getsize(tmp) > 0:
                        data = Path(tmp).read_bytes()
                        return data
                    last_error = RuntimeError(f"curl exit {r.returncode}: {r.stderr.decode('utf-8', 'replace')[:300]}")
                finally:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        log(f"下载 MinerU 结果失败，重试 {attempt}/5: {last_error}")
        time.sleep(5 * attempt)
    raise RuntimeError(f"下载 MinerU 结果失败: {last_error}")


def safe_extract_zip(data: bytes, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    root = output_dir.resolve()
    total_size = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        entries = zf.infolist()
        if len(entries) > MAX_ZIP_ENTRIES:
            raise RuntimeError(f"MinerU ZIP has too many entries ({len(entries)} > {MAX_ZIP_ENTRIES}).")
        for item in entries:
            if item.is_dir():
                continue
            name = item.filename.replace("\\", "/")
            member = PureWindowsPath(name)
            if (
                not name
                or "\x00" in name
                or name.startswith("/")
                or member.is_absolute()
                or member.drive
                or ".." in member.parts
            ):
                continue
            if item.file_size > MAX_ZIP_ENTRY_BYTES:
                raise RuntimeError(f"MinerU ZIP entry is too large: {name}")
            total_size += item.file_size
            if total_size > MAX_ZIP_TOTAL_BYTES:
                raise RuntimeError(f"MinerU ZIP expands beyond {MAX_ZIP_TOTAL_BYTES // (1024 * 1024)} MiB.")
            target = (root / Path(*member.parts)).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(item) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def find_markdown(root: Path, base_name: str) -> Path | None:
    candidates = []
    low_base = base_name.lower()
    for path in root.rglob("*.md"):
        rel = str(path.relative_to(root)).replace("\\", "/").lower()
        name = path.name.lower()
        if low_base in rel:
            rank = 0 if name == "full.md" else 1 if name == "document.md" else 10
        elif name in {"full.md", "document.md"}:
            rank = 20
        else:
            rank = 100
        if rank < 100:
            candidates.append((rank, len(str(path)), path))
    if not candidates:
        return None
    return sorted(candidates)[0][2]


def rewrite_chunk_image_paths(markdown: str, chunk_name: str) -> str:
    def rewrite(ref: str) -> str:
        original = ref
        path, sep, anchor = ref.partition("#")
        plain = urllib.parse.unquote(path.split("?", 1)[0]).replace("\\", "/").lstrip("./")
        if re.match(r"^(https?:|data:|#)", plain, flags=re.I):
            return original
        match = re.match(r"^(images?|assets|figures)/(.+)$", plain, flags=re.I)
        if match:
            return f"images/{chunk_name}/{match.group(2)}" + (f"#{anchor}" if sep else "")
        return original

    markdown = re.sub(
        r"!\[([^\]]*)\]\(([^)]+)\)",
        lambda m: f"![{m.group(1)}]({rewrite(m.group(2).strip())})",
        markdown,
    )
    markdown = re.sub(
        r"!\[\[([^\]|#]+)([^\]]*)\]\]",
        lambda m: f"![[{rewrite(m.group(1).strip())}{m.group(2)}]]",
        markdown,
    )
    return markdown


def copy_rich_images(src_chunk_dir: Path, dest_images_dir: Path) -> None:
    for folder_name in ("images", "image", "assets", "figures"):
        src = src_chunk_dir / folder_name
        if src.exists() and src.is_dir():
            dest_images_dir.mkdir(parents=True, exist_ok=True)
            for child in src.rglob("*"):
                if child.is_file():
                    rel = child.relative_to(src)
                    target = dest_images_dir / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(child, target)


def clear_mineru_cache(output_dir: Path) -> None:
    """Remove only artifacts owned by the MinerU generation step.

    The output directory also contains the user's source PDF and split/report
    files.  A force rebuild must never delete those unrelated files.
    """
    owned = {
        "full.md",
        "00-MinerU原始分块",
        "00-MinerU分块计划.json",
        "images",
    }
    for name in owned:
        target = output_dir / name
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()


def copy_full_md_input(source: Path, target: Path, force: bool) -> Path:
    """Copy an external full.md into the book folder with explicit conflict semantics."""
    source = source.resolve()
    target = target.resolve()
    if source == target:
        return target
    if target.exists():
        same_content = (
            target.is_file()
            and source.is_file()
            and target.stat().st_size == source.stat().st_size
            and sha256_file(target) == sha256_file(source)
        )
        if same_content:
            return target
        if not force:
            raise RuntimeError(
                f"Refusing to replace existing {target}; pass --force to use the supplied full.md."
            )
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target


def run_mineru_official(
    pdf_path: Path,
    base_name: str,
    output_dir: Path,
    settings: dict,
    page_count: int,
    outline_nodes: list[StructureNode] | None = None,
    force: bool = False,
    direct_download: bool = False,
) -> Path:
    base_output = output_dir
    full_path = base_output / "full.md"
    if full_path.exists() and full_path.stat().st_size > 0 and not force:
        log(f"Reusing existing MinerU output: {full_path}")
        return full_path
    token = resolve_token(settings["apiTokenEnv"])
    if force:
        clear_mineru_cache(base_output)
    try:
        base_output.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    ranges, chunk_plan = semantic_page_ranges(page_count, settings["pageRangeSize"], outline_nodes)
    (base_output / "00-MinerU分块计划.json").write_text(
        json.dumps({"pageCount": page_count, "ranges": ranges, **chunk_plan}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    log(f"MinerU official mode, {page_count} pages, {len(ranges)} semantic chunk(s): {chunk_plan['method']}.")

    # Keep raw MinerU artifacts for page-level diagnosis, reproducibility,
    # and resumable retries after transient API failures.
    chunk_root = base_output / "00-MinerU原始分块"
    chunk_root.mkdir(parents=True, exist_ok=True)
    merged_parts: list[str] = []
    try:
        for index, page_range in enumerate(ranges, start=1):
            chunk_name = f"chunk-{index:03d}-{page_range[0]}-{page_range[1]}"
            chunk_dir = chunk_root / chunk_name
            md_path = find_markdown(chunk_dir, base_name) if chunk_dir.exists() else None
            if md_path:
                log(f"Reusing completed chunk {index}/{len(ranges)} pages {page_range[0]}-{page_range[1]}.")
            else:
                if chunk_dir.exists():
                    shutil.rmtree(chunk_dir)
                log(f"Submitting chunk {index}/{len(ranges)} pages {page_range[0]}-{page_range[1]}.")
                batch_id, upload_url = request_upload_url(token, pdf_path, pdf_path.name, page_range, settings)
                upload_pdf(upload_url, pdf_path)
                item = poll_result(token, batch_id)
                safe_extract_zip(download_file(str(item["full_zip_url"]), direct=direct_download), chunk_dir)
                md_path = find_markdown(chunk_dir, base_name)
                if not md_path:
                    raise RuntimeError(f"MinerU chunk result missing Markdown: {chunk_name}")
                (chunk_dir / ".mineru-complete.json").write_text(
                    json.dumps({"pageRange": page_range, "markdown": str(md_path.relative_to(chunk_dir))}, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
            copy_rich_images(chunk_dir, base_output / "images" / chunk_name)
            md = md_path.read_text(encoding="utf-8", errors="replace")
            md = rewrite_chunk_image_paths(md, chunk_name)
            merged_parts.append(
                f"<!-- MinerU {chunk_name} pages {page_range[0]}-{page_range[1]} -->\n\n{md.strip()}"
            )
        full_path = base_output / "full.md"
        full_path.write_text("\n\n".join(merged_parts).strip() + "\n", encoding="utf-8")
        return full_path
    finally:
        # Deliberately retain raw ZIP output, Markdown, and layout JSON under
        # 00-MinerU原始分块. Later split validation must be traceable to them.
        pass


def parse_toc_chapters(markdown: str) -> list[tuple[str, str]]:
    lines = markdown.splitlines()
    toc_index = next((i for i, line in enumerate(lines) if re.match(r"^\s*#{1,6}\s*目录\s*$", line)), -1)
    if toc_index < 0:
        return []
    body_first_section = -1
    for i in range(toc_index + 1, len(lines)):
        if re.match(r"^\s*#{1,6}\s*第一节(?:\s|[|｜])", lines[i]):
            body_first_section = i
            break
    end = body_first_section if body_first_section > toc_index else min(len(lines), toc_index + 500)
    toc_lines = lines[toc_index + 1 : end]
    pattern = re.compile(
        rf"^\s*#{{0,6}}\s*(第[{CHINESE_NUM}0-9]+章)\s*[|｜\s　、:：.．-]*(.+?)\s*(?:[.．·•…\s]+[0-9]{{1,4}})?\s*$"
    )
    chapters: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in toc_lines:
        text = line.strip()
        match = pattern.match(text)
        if not match:
            continue
        number = clean_title(match.group(1))
        title = clean_title(match.group(2))
        if not valid_title(title):
            continue
        key = f"{number}:{title}"
        if key in seen:
            continue
        seen.add(key)
        chapters.append((number, title))
    return chapters


def clean_title(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "").replace("\u0000", " ")).strip()
    value = re.sub(r"\s*[.．·•…]{2,}\s*\d{1,4}\s*$", "", value)
    value = re.sub(r"\s+\d{1,4}\s*$", "", value)
    return value.strip(" |｜\t")


def valid_title(value: str) -> bool:
    if not value or len(value) > 60:
        return False
    if re.search(r"[。；;,，]", value):
        return False
    if re.match(r"^[)）】\]、,，.;；。]", value):
        return False
    return True


def find_body_first_sections(markdown: str) -> list[int]:
    lines = markdown.splitlines(keepends=True)
    starts: list[int] = []
    offset = 0
    toc_seen = False
    toc_done = False
    for line in lines:
        plain = line.strip()
        if re.match(r"^#{1,6}\s*目录\s*$", plain):
            toc_seen = True
        elif toc_seen and re.match(r"^#{1,6}\s*第一节(?:\s|[|｜])", plain):
            toc_done = True
        if toc_done and re.match(r"^#{1,6}\s*第一节(?:\s|[|｜])", plain):
            starts.append(paragraph_start_before(markdown, offset))
        offset += len(line)
    return dedupe_close_positions(starts)


def paragraph_start_before(markdown: str, heading_offset: int) -> int:
    window_start = max(0, heading_offset - 1500)
    prefix = markdown[window_start:heading_offset]
    blocks = list(re.finditer(r"(^|\n\s*\n)([^\n][\s\S]*?)(?=\n\s*\n|$)", prefix))
    for match in reversed(blocks):
        block = match.group(2).strip()
        if 20 <= len(block) <= 1000 and not re.match(r"^(!?\[|<table|#+|图\d|表\d|第.+章|目录)", block):
            return window_start + match.start(2)
    return heading_offset


def dedupe_close_positions(positions: Iterable[int]) -> list[int]:
    out: list[int] = []
    for pos in sorted(set(positions)):
        if out and pos - out[-1] < 1000:
            continue
        out.append(pos)
    return out


def fallback_heading_chapters(markdown: str) -> list[Chapter]:
    pattern = re.compile(
        rf"(^|\n)\s*#{{1,6}}\s*(第[{CHINESE_NUM}0-9]+章)\s*[|｜\s　、:：.．-]*(.+?)\s*(?=\n)",
        re.M,
    )
    matches = []
    for m in pattern.finditer(markdown):
        number = clean_title(m.group(2))
        title = clean_title(m.group(3))
        if valid_title(title):
            matches.append((m.start(0) + (1 if m.group(1) else 0), number, title))
    if len(matches) < 2:
        return []
    body_floor = 0
    toc = re.search(r"(^|\n)#{1,6}\s*目录\s*(\n|$)", markdown)
    if toc:
        first_section = re.search(r"(^|\n)#{1,6}\s*第一节(?:\s|[|｜])", markdown[toc.end() :])
        if first_section:
            body_floor = toc.end() + first_section.start()
    matches = [m for m in matches if m[0] >= body_floor]
    chapters: list[Chapter] = []
    for index, (start, number, title) in enumerate(matches):
        end = matches[index + 1][0] if index + 1 < len(matches) else len(markdown)
        content = markdown[start:end].strip()
        chapters.append(Chapter(number, title, start, end, content))
    return chapters


def split_by_size(markdown: str, size: int = 30000) -> list[Chapter]:
    if len(markdown) <= size:
        return [Chapter("1", "全文", 0, len(markdown), markdown.strip())]
    chapters: list[Chapter] = []
    start = 0
    index = 1
    while start < len(markdown):
        end = min(start + size, len(markdown))
        if end < len(markdown):
            boundary = markdown.rfind("\n\n", start, end)
            if boundary > start + size // 2:
                end = boundary
        chapters.append(
            Chapter(str(index), f"第 {index} 部分", start, end, markdown[start:end].strip())
        )
        start = end
        index += 1
    total = len(chapters)
    for idx, ch in enumerate(chapters, start=1):
        ch.title = f"第 {idx}/{total} 部分"
    return chapters


def _cn_number(n: int) -> str:
    """Convert an integer to Chinese numerals up to 99 (for chapter/extra labels)."""
    digits = "零一二三四五六七八九"
    if n <= 10:
        return "十" if n == 10 else digits[n]
    if n < 20:
        return "十" + digits[n - 10]
    if n < 100:
        tens, ones = n // 10, n % 10
        return digits[tens] + "十" + (digits[ones] if ones else "")
    return str(n)


def repair_toc_body_separation(markdown: str) -> str:
    """Repair MinerU output where the printed table of contents and the body are
    separated and the body is missing its `第X章` chapter headings.

    Some books render a TOC page whose `## 第X章` headings are followed only by a
    section outline, while the body itself begins with `## 第一节 ...` and never
    repeats the chapter title. Naive heading splitting then dumps the whole book
    under one chapter (or produces a "table of contents only" result). This
    function detects that pattern and re-inserts the correct `## 第X章 标题`
    heading before each body chapter, using the TOC as the source of truth.

    The repair only triggers when the specific broken pattern is detected, so
    normal books (whose body already carries `第X章` headings) are returned
    unchanged.
    """
    lines = markdown.split("\n")
    toc_line = next((i for i, line in enumerate(lines) if re.match(r"^\s*#{1,6}\s*目录\s*$", line)), -1)
    if toc_line < 0:
        return markdown
    # TOC must advertise at least one 第X章 chapter.
    toc_chapter_re = re.compile(r"^\s*#+\s*第([零一二三四五六七八九十百]+)章\s*[|｜\s　、:：.．·•\-]*(.*)$")
    plain_section_re = re.compile(r"^\s*第([零一二三四五六七八九十百]+)节\s*(.+?)\s*$")
    body_section_re = re.compile(r"^\s*#+\s*第([零一二三四五六七八九十百]+)节\s*[|｜]?\s*(.+?)\s*$")

    toc: list[dict] = []
    cur: dict | None = None
    for ln in lines[toc_line + 1 :]:
        m = toc_chapter_re.match(ln.strip())
        if m:
            cur = {
                "number": "第" + m.group(1) + "章",
                "title": clean_title(m.group(2)),
                "sections": [],
            }
            toc.append(cur)
            continue
        if cur is None:
            continue
        sm = plain_section_re.match(ln.strip())
        if sm:
            name = clean_title(sm.group(2))
            if name:
                cur["sections"].append(name)
    toc = [c for c in toc if c["title"] and len(c["title"]) <= 40]
    if not toc:
        return markdown

    # Body starts at the first '## 第一节' after the TOC marker.
    body_start = -1
    for i in range(toc_line + 1, len(lines)):
        if re.match(r"^\s*#+\s*第一节(?:\s|[|｜])", lines[i].strip()):
            body_start = i
            break
    if body_start < 0:
        return markdown

    # If the body region already contains 第X章 headings, the structure is fine.
    body_text = "\n".join(lines[body_start:])
    if toc_chapter_re.search(body_text):
        return markdown

    front_matter = "\n".join(lines[:body_start]).strip()

    def norm(s: str) -> str:
        return re.sub(r"[\s|｜:：.．·•\-（）()、，,。；;0-9]+", "", s or "")

    def exact_eq(a: str, b: str) -> bool:
        a, b = norm(a), norm(b)
        if not a or not b:
            return False
        return a == b or a in b or b in a

    def resolve_first(name: str, current_idx: int) -> int:
        cands = [i for i, ch in enumerate(toc) if ch["sections"] and exact_eq(ch["sections"][0], name)]
        if not cands and "发展简史" in name:
            cands = [0]
        if not cands:
            return -1
        fwd = [c for c in cands if c > current_idx]
        return min(fwd) if fwd else min(cands)

    def resolve_section(name: str, current_idx: int) -> int:
        cands = [i for i, ch in enumerate(toc) if any(exact_eq(s, name) for s in ch["sections"])]
        if not cands:
            return -1
        fwd = [c for c in cands if c > current_idx]
        return min(fwd) if fwd else min(cands)

    def infer_extra_title(prose: str) -> str:
        for ln in prose.splitlines():
            t = ln.strip().lstrip("#").strip()
            if 2 <= len(t) <= 30 and not re.match(r"^[图表明表第0-9]", t) and not re.search(r"[。；;，,：:]", t):
                return t
        return "未命名补充章节"

    # Collect every body section heading with its absolute character offset.
    headings: list[tuple[int, str, str]] = []
    body_offset = len("\n".join(lines[:body_start])) + 1
    for m in re.finditer(r"(?m)^(.+)$", markdown):
        if m.start(1) < body_offset:
            continue
        line = m.group(1).strip()
        hm = body_section_re.match(line) or plain_section_re.match(line)
        if hm:
            headings.append((m.start(1), hm.group(1), clean_title(hm.group(2))))

    blocks: list[dict] = []
    current_idx = -1
    placed = 0
    extra_mode = False
    extra_count = 0
    for off, cn, name in headings:
        is_first = cn == "一"
        force_new = False
        if extra_mode:
            if not is_first:
                continue
            ci = -1
            force_new = True
        else:
            if is_first:
                ci = resolve_first(name, current_idx)
            else:
                if current_idx >= 0 and any(exact_eq(s, name) for s in toc[current_idx]["sections"]):
                    ci = current_idx
                else:
                    ci = resolve_section(name, current_idx)
        if force_new or ci != current_idx:
            if blocks:
                blocks[-1]["end"] = off
            if ci >= 0:
                num = toc[ci]["number"]
                title = toc[ci]["title"]
                placed += 1
                if placed >= len(toc):
                    extra_mode = True
            else:
                extra_count += 1
                num = "第" + _cn_number(18 + extra_count) + "章"
                title = infer_extra_title(markdown[off - 400 : off + 200])
            blocks.append({"toc_idx": ci, "num": num, "title": title, "start": off, "end": len(markdown)})
            current_idx = ci
    if blocks:
        blocks[-1]["end"] = len(markdown)
    blocks = [b for b in blocks if b["end"] > b["start"]]

    out_parts: list[str] = []
    if front_matter:
        out_parts.append(front_matter)
    for b in blocks:
        content = markdown[b["start"] : b["end"]].strip()
        if not content:
            continue
        out_parts.append(f"## {b['num']} {b['title']}\n\n{content}")
    return "\n\n\n".join(out_parts).strip() + "\n"


PART_HEAD_RE = re.compile(r"^#+\s*第[零一二三四五六七八九十百]+[部篇卷章]")
SECTION_RE  = re.compile(r"^#+\s*(?:\d{1,2}\s|[第零一二三四五六七八九十百]+节)")
PLAIN_SECTION_RE = re.compile(r"^第[零一二三四五六七八九十百]+节\s")


def normalize_headings(text: str) -> str:
    """Remap flat MinerU headings into a proper H2/H3/(H4) hierarchy.

    MinerU often emits every heading as '##' (H2), collapsing the real book
    structure. We detect the depth automatically:
      - If the content has sections (第X节 / numbered NN) → 3-level:
          part/chapter → H2, section → H3, sub-heading → H4
      - If the content has no sections (just 第X章 + sub-headings) → 2-level:
          chapter → H2, sub-heading → H3
      - Front matter (no part/chapter markers): demote lone H1→H2, keep rest.
    The chapter file's program-generated H1 title + H2 frame sits above this.
    """
    has_top = bool(re.search(r"^#+\s*第[零一二三四五六七八九十百]+[部篇卷章]", text, re.M))
    has_section = (
        (bool(SECTION_RE.search(text)) or bool(re.search(r"^第[零一二三四五六七八九十百]+节\s", text, re.M)))
        if has_top else False
    )
    sub_prefix = "####" if (has_top and has_section) else "###"
    out: list[str] = []
    for line in text.split("\n"):
        s = line.lstrip()
        if not s.startswith("#"):
            # Catch plain-text section headings that MinerU missed
            if has_top and has_section and PLAIN_SECTION_RE.match(s):
                out.append("### " + s.strip())
            else:
                out.append(line)
            continue
        level = len(s) - len(s.lstrip("#"))
        content = s.lstrip("#").strip()
        if has_top:
            if PART_HEAD_RE.match(s):
                out.append("## " + content)
            elif has_section and SECTION_RE.match(s):
                out.append("### " + content)
            else:
                out.append(sub_prefix + " " + content)
        else:
            out.append("## " + content if level == 1 else s)
    return "\n".join(out)


def split_by_chapters(markdown: str) -> list[Chapter]:
    """Split on 章 headings (e.g. '## 第一章 一个好点子').

    Used for books with numbered chapters but no 节 layer below them.
    """
    lines = markdown.splitlines(keepends=True)
    ch_re = re.compile(r"^#+\s*第([零一二三四五六七八九十百]+)章\s*[|｜\s　、:：.．·•\-]*(.*)$")
    points: list[tuple[int, str, str]] = []
    offset = 0
    for line in lines:
        m = ch_re.match(line.strip())
        if m:
            points.append((offset, "第" + m.group(1) + "章", clean_title(m.group(2))))
        offset += len(line)
    chapters: list[Chapter] = []
    first_off = points[0][0] if points else len(markdown)
    if first_off > 0:
        fm = markdown[:first_off].strip()
        if fm:
            chapters.append(Chapter("00", "前言与导论", 0, first_off, fm))
    for index, (off, label, title) in enumerate(points):
        end = points[index + 1][0] if index + 1 < len(points) else len(markdown)
        content = markdown[off:end].strip()
        chapters.append(Chapter(label, title or label, off, end, content))
    return chapters


def split_by_parts(markdown: str) -> list[Chapter]:
    """Coarse split on 部/篇 part headings (e.g. '## 第一部 两个系统').

    Each part becomes one chapter (a single Markdown file holding all its
    sections). Front matter before the first part is a leading chapter. This is
    the preferred granularity for e-books / smaller books where splitting every
    section would be too scattered.
    """
    lines = markdown.splitlines(keepends=True)
    part_re = re.compile(r"^#+\s*第([零一二三四五六七八九十百]+)部\s*[|｜\s　、:：.．·•\-]*(.*)$")
    points: list[tuple[int, str, str]] = []
    offset = 0
    for line in lines:
        m = part_re.match(line.strip())
        if m:
            points.append((offset, "第" + m.group(1) + "部", clean_title(m.group(2))))
        offset += len(line)
    chapters: list[Chapter] = []
    first_off = points[0][0] if points else len(markdown)
    if first_off > 0:
        fm = markdown[:first_off].strip()
        if fm:
            chapters.append(Chapter("00", "前言与导论", 0, first_off, fm))
    for index, (off, label, title) in enumerate(points):
        end = points[index + 1][0] if index + 1 < len(points) else len(markdown)
        content = markdown[off:end].strip()
        chapters.append(Chapter(label, title or label, off, end, content))
    return chapters


def split_by_sections(markdown: str) -> list[Chapter]:
    """Granular split on 部 + 节 (sections), e.g. '## 01 故事中的人物'.

    Used for books organized as '第X部' with numbered '## NN 标题' sections but
    no standard 章/第一节 structure. Falls back to the part-level split when
    fewer than two numbered sections are found.
    """
    lines = markdown.splitlines(keepends=True)
    part_re = re.compile(r"^#+\s*第([零一二三四五六七八九十百]+)部\s*[|｜\s　、:：.．·•\-]*(.*)$")
    sec_re = re.compile(r"^#+\s*(\d{1,2})\s+(.+)$")
    points: list[tuple[int, str, str, str]] = []  # (offset, type, label, title)
    offset = 0
    for line in lines:
        s = line.strip()
        m = part_re.match(s)
        if m:
            points.append((offset, "part", "第" + m.group(1) + "部", clean_title(m.group(2))))
            offset += len(line)
            continue
        m = sec_re.match(s)
        if m:
            points.append((offset, "sec", m.group(1), clean_title(m.group(2))))
        offset += len(line)
    chapters: list[Chapter] = []
    first_off = points[0][0] if points else len(markdown)
    if first_off > 0:
        fm = markdown[:first_off].strip()
        if fm:
            chapters.append(Chapter("00", "前言与导论", 0, first_off, fm))
    sec_list: list[tuple[int, str, str, str]] = []
    cur_part = ""
    for (off, typ, label, title) in points:
        if typ == "part":
            cur_part = label
        else:
            sec_list.append((off, cur_part, label, title))
    if len(sec_list) < 2:
        return split_by_parts(markdown)
    for index, (off, part, num, title) in enumerate(sec_list):
        end = sec_list[index + 1][0] if index + 1 < len(sec_list) else len(markdown)
        number = f"{part} {num.zfill(2)}" if part else num.zfill(2)
        content = markdown[off:end].strip()
        chapters.append(Chapter(number, title, off, end, content))
    return chapters


STRUCTURE_LABELS = {
    "部": "part",
    "篇": "part",
    "编": "part",
    "卷": "part",
    "单元": "part",
    "模块": "part",
    "专题": "part",
    "章": "chapter",
    "课": "chapter",
    "讲": "chapter",
    "项目": "chapter",
    "任务": "chapter",
    "实验": "chapter",
    "案例": "chapter",
    "小节": "section",
    "节": "section",
    "目": "section",
}
STRUCTURE_ORDER = {"part": 1, "chapter": 2, "section": 3, "numbered": 4, "appendix": 5}
NUM_TOKEN = r"零〇一二两三四五六七八九十百千0-9IVXLCDMivxlcdm"
EXPLICIT_STRUCTURE_RE = re.compile(
    rf"^(?P<number>第\s*[{NUM_TOKEN}]+)\s*(?P<label>单元|模块|专题|项目|任务|实验|案例|小节|部|篇|编|卷|章|课|讲|节|目)"
    r"\s*[|｜、:：.．·•\-—–]*\s*(?P<title>.*)$"
)
DECIMAL_HEADING_RE = re.compile(r"^(?P<number>\d+(?:\.\d+){0,5})[、.．)]?\s+(?P<title>.+)$")
CN_LIST_HEADING_RE = re.compile(rf"^(?P<number>[（(][{NUM_TOKEN}]+[）)]|[{NUM_TOKEN}]+[、.．])\s*(?P<title>.+)$")
SPECIAL_HEADING_RE = re.compile(r"^(?P<title>前言|序言|引言|绪论|附录(?:\s*[A-Z0-9一二三四五六七八九十])?.*|参考文献|索引|后记)\s*$")
PAGE_MARKER_RE = re.compile(r"<!--\s*(?:page|page_number|页码)\s*[:=]\s*(\d+)\s*-->", re.I)


def normalize_match_text(value: str) -> str:
    value = str(value or "").lower().replace("第", "")
    return re.sub(r"[\s|｜、:：.．·•\-—–_（）()【】\[\]0-9]", "", value)


def normalize_number(value: str) -> str:
    return re.sub(r"[\s第、.．()（）]", "", str(value or "").lower())


def line_start_at(markdown: str, index: int) -> int:
    return markdown.rfind("\n", 0, max(0, index)) + 1


def parse_toc_page(title: str) -> tuple[str, int | None]:
    match = re.search(r"(?:[.．·•…]|\s){2,}(\d{1,4})\s*$", title)
    if not match:
        return title.strip(), None
    return title[: match.start()].strip(), int(match.group(1))


def parse_structure_heading(
    raw_line: str,
    start: int,
    line: int,
    source: str,
    page: int | None,
    allow_plain: bool,
) -> StructureNode | None:
    stripped = raw_line.strip()
    hashed = bool(re.match(r"^#{1,6}\s+", stripped))
    text = re.sub(r"^#{1,6}\s+", "", stripped).strip()
    if not text:
        return None
    special = SPECIAL_HEADING_RE.match(text)
    if special and (hashed or allow_plain):
        return StructureNode("", "", special.group("title"), "appendix", 1, start, line, page, source=source)
    match = EXPLICIT_STRUCTURE_RE.match(text)
    if match:
        title, toc_page = parse_toc_page(clean_title(match.group("title")))
        label = match.group("label")
        return StructureNode(
            "",
            re.sub(r"\s+", "", match.group("number")) + label,
            title or re.sub(r"\s+", "", match.group("number")) + label,
            STRUCTURE_LABELS[label],
            STRUCTURE_ORDER[STRUCTURE_LABELS[label]],
            start,
            line,
            page,
            toc_page=toc_page,
            source=source,
        )
    if not (hashed or allow_plain):
        return None
    match = DECIMAL_HEADING_RE.match(text)
    if match:
        title, toc_page = parse_toc_page(clean_title(match.group("title")))
        number = match.group("number")
        return StructureNode("", number, title, "numbered", number.count(".") + 1, start, line, page, toc_page, source)
    match = CN_LIST_HEADING_RE.match(text)
    if match:
        title, toc_page = parse_toc_page(clean_title(match.group("title")))
        return StructureNode("", match.group("number"), title, "numbered", 1, start, line, page, toc_page, source)
    return None


def decode_pdf_outline_title(value: object) -> str:
    """Decode PDF bookmark titles from their original UTF-16 bytes when available."""
    raw = getattr(value, "original_bytes", None)
    if raw:
        try:
            if raw.startswith(b"\xfe\xff"):
                return raw[2:].decode("utf-16-be", "replace").strip()
            if raw.startswith(b"\xff\xfe"):
                return raw[2:].decode("utf-16-le", "replace").strip()
        except UnicodeDecodeError:
            pass
    return str(value or "").strip()


def clean_pdf_outline_title(value: str) -> str:
    """Remove common page-range artifacts found in exported PDF bookmark labels."""
    value = value.replace("|", " ")
    value = re.sub(r"\s*/\s*\d{1,4}\s+\d{1,4}\s*$", "", value)
    value = re.sub(r"\s*/\s*$", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    if re.match(rf"^第\s*[{NUM_TOKEN}]+\s*(?:部|篇|编|卷)", value):
        value = re.sub(r"(?<=\D)\d(?:\s+\d){1,3}\s*$", "", value).strip()
    return value


def extract_pdf_outline(pdf_path: Path) -> list[StructureNode]:
    """Read the PDF's bookmarks as the primary TOC framework."""
    reader = PdfReader(str(pdf_path))
    outline = reader.outline
    nodes: list[StructureNode] = []

    def visit(items: list[object], depth: int = 0) -> None:
        for item in items:
            if isinstance(item, list):
                visit(item, depth + 1)
                continue
            raw_title = decode_pdf_outline_title(item.get("/Title", ""))
            title = clean_pdf_outline_title(raw_title)
            if not title:
                continue
            try:
                page = reader.get_destination_page_number(item) + 1
            except Exception:
                page = None
            parsed = parse_structure_heading(
                f"# {title}",
                0,
                0,
                "pdf-outline",
                page,
                allow_plain=True,
            )
            if parsed is None:
                parsed = StructureNode(
                    "",
                    "",
                    title,
                    "numbered",
                    max(1, depth + 1),
                    0,
                    0,
                    page,
                    source="pdf-outline",
                )
            parsed.logical_level = max(parsed.logical_level, depth + 1)
            parsed.source = "pdf-outline"
            nodes.append(parsed)

    visit(outline)
    assign_node_ids(nodes, "pdf-outline")
    return nodes


def collect_chunk_page_marks(markdown: str, raw_chunk_dir: Path | None) -> list[tuple[int, int]]:
    """Recover source PDF page anchors from retained MinerU content lists.

    MinerU can classify chapter titles as page headers and omit them from
    full.md. Its content lists still retain per-page body text, which gives us
    stable anchors for mapping PDF bookmarks back into the merged Markdown.
    """
    if not raw_chunk_dir or not raw_chunk_dir.exists():
        return []
    marks: list[tuple[int, int]] = []
    for chunk_dir in sorted(path for path in raw_chunk_dir.iterdir() if path.is_dir()):
        match = re.fullmatch(r"chunk-\d+-(\d+)-(\d+)", chunk_dir.name)
        if not match:
            continue
        content_list = next(chunk_dir.glob("*_content_list.json"), None)
        if content_list is None:
            continue
        try:
            items = json.loads(content_list.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log(f"Could not read MinerU content list {content_list}: {exc}")
            continue
        if not isinstance(items, list):
            continue
        start_page = int(match.group(1))
        marker = f"<!-- MinerU {chunk_dir.name} pages "
        cursor = markdown.find(marker)
        cursor = cursor if cursor >= 0 else 0
        pages: dict[int, list[str]] = {}
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            page_index = item.get("page_idx")
            text = str(item.get("text") or "").strip()
            if not isinstance(page_index, int) or len(text) < 24:
                continue
            pages.setdefault(page_index, []).append(text)
        for page_index in sorted(pages):
            for text in pages[page_index]:
                anchor = text[:120]
                position = markdown.find(anchor, cursor)
                if position < 0:
                    continue
                marks.append((line_start_at(markdown, position), start_page + page_index))
                cursor = position + len(anchor)
                break
    return sorted(set(marks))


def collect_page_marks(
    markdown: str,
    layout_json: Path | None = None,
    extra_marks: list[tuple[int, int]] | None = None,
) -> list[tuple[int, int]]:
    marks = [(match.start(), int(match.group(1))) for match in PAGE_MARKER_RE.finditer(markdown)]
    marks.extend(extra_marks or [])
    if layout_json and layout_json.exists():
        try:
            data = json.loads(layout_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log(f"Could not read layout JSON for page hints: {exc}")
            data = None

        hints: list[tuple[str, int]] = []
        def visit(value: object) -> None:
            if isinstance(value, dict):
                page_value = next((value.get(key) for key in ("page_num", "pageNum", "page", "page_index") if value.get(key) is not None), None)
                text_value = next((value.get(key) for key in ("text", "content", "markdown", "markdownContent") if isinstance(value.get(key), str)), "")
                if page_value is not None and len(str(text_value).strip()) >= 8:
                    try:
                        page = int(page_value) + (1 if value.get("page_index") is not None else 0)
                        hints.append((str(text_value).strip()[:80], page))
                    except (TypeError, ValueError):
                        pass
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        if data is not None:
            visit(data)
            cursor = 0
            for hint, page in hints:
                pos = markdown.find(hint, cursor)
                if pos < 0:
                    pos = markdown.find(hint)
                if pos >= 0:
                    marks.append((pos, page))
                    cursor = pos + len(hint)
    return sorted(set(marks))


def page_at(marks: list[tuple[int, int]], index: int) -> int | None:
    result: int | None = None
    for position, page in marks:
        if position > index:
            break
        result = page
    return result


def find_toc_region(markdown: str) -> tuple[int, int] | None:
    toc_match = re.search(r"(?m)^#{1,6}\s*目录\s*$", markdown)
    if not toc_match:
        return None
    toc_start = toc_match.end()
    seen: set[tuple[str, str]] = set()
    offset = toc_start
    for line_no, raw_line in enumerate(markdown[toc_start:].splitlines(keepends=True), start=markdown.count("\n", 0, toc_start) + 2):
        node = parse_structure_heading(raw_line, offset, line_no, "toc", None, allow_plain=True)
        if node and node.title:
            signature = (normalize_number(node.number), normalize_match_text(node.title))
            if signature in seen and offset - toc_start > 20:
                return toc_start, offset
            seen.add(signature)
        offset += len(raw_line)
        if offset - toc_start > 120000:
            break
    return toc_start, toc_start


def assign_node_ids(nodes: list[StructureNode], prefix: str) -> None:
    counters: dict[int, int] = {}
    for index, node in enumerate(nodes, start=1):
        level = max(1, node.logical_level)
        counters[level] = counters.get(level, 0) + 1
        for deeper in [key for key in counters if key > level]:
            counters.pop(deeper, None)
        path = ".".join(str(counters[key]) for key in sorted(counters) if key <= level)
        node.node_id = f"{prefix}-{path or index}"


def collect_structure_nodes(
    markdown: str,
    layout_json: Path | None = None,
    page_marks: list[tuple[int, int]] | None = None,
) -> tuple[list[StructureNode], list[StructureNode], int, list[tuple[int, int]]]:
    marks = collect_page_marks(markdown, layout_json, page_marks)
    region = find_toc_region(markdown)
    toc_nodes: list[StructureNode] = []
    body_nodes: list[StructureNode] = []
    toc_start, body_start = region if region else (-1, 0)
    offset = 0
    for line_no, raw_line in enumerate(markdown.splitlines(keepends=True), start=1):
        in_toc = toc_start >= 0 and toc_start <= offset < body_start
        in_body = offset >= body_start
        if in_toc or in_body:
            node = parse_structure_heading(raw_line, offset, line_no, "toc" if in_toc else "body", page_at(marks, offset), allow_plain=in_toc or not raw_line.lstrip().startswith("#"))
            if node and node.title:
                (toc_nodes if in_toc else body_nodes).append(node)
        offset += len(raw_line)
    assign_node_ids(toc_nodes, "toc")
    assign_node_ids(body_nodes, "body")
    return toc_nodes, body_nodes, body_start, marks


def target_kind(split_by: str, toc_nodes: list[StructureNode], body_nodes: list[StructureNode]) -> str:
    if split_by != "auto":
        return split_by
    candidates = toc_nodes or body_nodes
    counts = {kind: sum(node.kind == kind for node in candidates) for kind in ("chapter", "section", "part", "numbered")}
    if counts["chapter"] >= 2:
        return "chapter"
    if counts["section"] >= 2:
        return "section"
    if counts["part"] >= 2:
        return "part"
    return "numbered"


def nodes_for_kind(nodes: list[StructureNode], kind: str) -> list[StructureNode]:
    if kind == "part":
        return [node for node in nodes if node.kind == "part"]
    if kind == "chapter":
        return [node for node in nodes if node.kind == "chapter"]
    if kind == "section":
        return [node for node in nodes if node.kind == "section"]
    return [node for node in nodes if node.kind == "numbered"]


def node_match_score(toc: StructureNode, body: StructureNode) -> float:
    if toc.kind != body.kind:
        return 0.0
    number_score = 1.0 if normalize_number(toc.number) and normalize_number(toc.number) == normalize_number(body.number) else 0.0
    title_score = SequenceMatcher(None, normalize_match_text(toc.title), normalize_match_text(body.title)).ratio()
    if not normalize_match_text(toc.title) or not normalize_match_text(body.title):
        title_score = 0.0
    return number_score * 0.55 + title_score * 0.45


def align_toc_to_body(toc_nodes: list[StructureNode], body_nodes: list[StructureNode]) -> tuple[list[tuple[StructureNode, StructureNode, float]], list[StructureNode]]:
    aligned: list[tuple[StructureNode, StructureNode, float]] = []
    unmatched: list[StructureNode] = []
    cursor = 0
    for toc in toc_nodes:
        best: tuple[int, float] | None = None
        for index in range(cursor, min(len(body_nodes), cursor + 120)):
            score = node_match_score(toc, body_nodes[index])
            if best is None or score > best[1]:
                best = (index, score)
        if best is None or best[1] < 0.64:
            unmatched.append(toc)
            continue
        index, score = best
        aligned.append((toc, body_nodes[index], score))
        cursor = index + 1
    return aligned, unmatched


def align_outline_chapters_to_markdown(
    markdown: str,
    outline_nodes: list[StructureNode],
    toc_targets: list[StructureNode],
    page_marks: list[tuple[int, int]],
) -> tuple[list[tuple[StructureNode, StructureNode, float]], list[StructureNode]]:
    """Match PDF chapter bookmarks to Markdown headings that omit chapter labels.

    MinerU frequently renders a chapter as ``## Chapter title`` rather than
    ``## Chapter N Chapter title``.  The general structure matcher classifies
    those headings as generic numbered nodes, so use the bookmark order and
    heading text directly for this specific case.
    """
    parents: dict[str, StructureNode] = {}
    stack: list[StructureNode] = []
    for node in outline_nodes:
        while stack and stack[-1].logical_level >= node.logical_level:
            stack.pop()
        if stack:
            parents[node.node_id] = stack[-1]
        stack.append(node)

    # Include ordinary nonblank lines as well as Markdown headings.  In this
    # book MinerU drops some chapter headings entirely, but the first prose
    # line still starts with the chapter title.
    candidates: list[StructureNode] = []
    heading_starts: set[int] = set()
    offset = 0
    for line_no, raw_line in enumerate(markdown.splitlines(keepends=True), start=1):
        stripped = raw_line.strip()
        if stripped:
            if re.match(r"^#{1,6}\s+", raw_line):
                heading_starts.add(offset)
            parsed = parse_structure_heading(raw_line, offset, line_no, "body", None, allow_plain=True)
            title = parsed.title if parsed else clean_title(re.sub(r"^#{1,6}\s+", "", stripped))
            candidates.append(StructureNode("", "", title, "numbered", 1, offset, line_no, page_at(page_marks, offset)))
        offset += len(raw_line)

    def key(value: str) -> str:
        return normalize_match_text(value)

    # The rendered TOC is also made of Markdown headings.  The first top-level
    # part appears twice: once in that TOC and once where the actual body
    # begins.  Discard the first occurrence so chapter anchors cannot bind to
    # the TOC instead of chapter text.
    top_level = min((node.logical_level for node in outline_nodes), default=1)
    first_part = next((node for node in outline_nodes if node.logical_level == top_level), None)
    if first_part:
        starts = [
            item.start
            for item in candidates
            if item.start in heading_starts and key(item.title) == key(first_part.title)
        ]
        if len(starts) >= 2:
            candidates = [item for item in candidates if item.start >= starts[1]]

    aligned: list[tuple[StructureNode, StructureNode, float]] = []
    unmatched: list[StructureNode] = []
    cursor = 0
    for toc in toc_targets:
        expected = key(toc.title)
        parent = parents.get(toc.node_id)
        parent_expected = key(parent.title) if toc.title == "总论" and parent else ""
        best: tuple[int, float] | None = None
        # Source-page anchors derived from MinerU content lists are more reliable
        # than a title string: a title may be omitted from Markdown or appear
        # later in a cross-reference, table, or running header.
        if toc.page is not None:
            for index in range(cursor, len(candidates)):
                candidate = candidates[index]
                if candidate.page != toc.page:
                    continue
                best = (index, 0.9)
                break
        if best is None:
            for index in range(cursor, len(candidates)):
                candidate = candidates[index]
                candidate_key = key(candidate.title)
                score = 0.0
                if expected and candidate_key == expected:
                    score = 1.0
                elif parent_expected and candidate_key == parent_expected:
                    score = 0.99
                elif expected and candidate_key.startswith(expected):
                    score = 0.96
                elif expected and candidate.title.startswith("#") and SequenceMatcher(None, expected, candidate_key).ratio() >= 0.92:
                    score = 0.92
                if score:
                    best = (index, score)
                    break
        if best is None:
            unmatched.append(toc)
            continue
        index, score = best
        aligned.append((toc, candidates[index], score))
        cursor = index + 1
    return aligned, unmatched


def chapter_order_key(chapter: Chapter) -> tuple[int, int]:
    """Keep generated folders in source-page order, with Markdown position as fallback."""
    page = chapter.page_start if chapter.page_start is not None else 10**9
    return page, chapter.start


def build_split_plan(
    markdown: str,
    split_by: str = "auto",
    layout_json: Path | None = None,
    outline_nodes: list[StructureNode] | None = None,
    page_marks: list[tuple[int, int]] | None = None,
) -> SplitPlan:
    markdown_toc_nodes, body_nodes, body_start, resolved_page_marks = collect_structure_nodes(
        markdown, layout_json, page_marks
    )
    toc_nodes = outline_nodes or markdown_toc_nodes
    kind = target_kind(split_by, toc_nodes, body_nodes)
    toc_targets = nodes_for_kind(toc_nodes, kind)
    body_targets = nodes_for_kind(body_nodes, kind)
    reasons: list[str] = []
    chapters: list[Chapter] = []
    unmatched: list[StructureNode] = []
    method = f"pdf-bookmarks:{kind}" if outline_nodes else f"structure-tree:{kind}"
    if toc_targets and (body_targets or (outline_nodes and kind == "chapter" and resolved_page_marks)):
        if outline_nodes and kind == "chapter":
            aligned, unmatched = align_outline_chapters_to_markdown(
                markdown, outline_nodes, toc_targets, resolved_page_marks
            )
        else:
            aligned, unmatched = align_toc_to_body(toc_targets, body_targets)
        if len(aligned) < 2:
            reasons.append("PDF 书签/目录与正文的可靠匹配不足 2 项，已退回正文标题拆分。" if outline_nodes else "目录与正文的可靠匹配不足 2 项，已退回正文标题拆分。")
            aligned = []
        elif unmatched:
            reasons.append(f"目录中有 {len(unmatched)} 个 {kind} 节点未能在正文可靠定位。")
        for toc, body, score in aligned:
            chapters.append(
                Chapter(
                    toc.number,
                    toc.title,
                    body.start,
                    len(markdown),
                    "",
                    node_id=toc.node_id,
                    kind=kind,
                    logical_level=toc.logical_level,
                    confidence=score,
                    needs_review=score < 0.82,
                    review_reasons=["目录标题与正文标题为模糊匹配。"] if score < 0.82 else [],
                    page_start=body.page,
                )
            )
    if not chapters:
        method = f"body-headings:{kind}"
        if len(body_targets) < 2:
            legacy = split_by_size(markdown) if split_by == "auto" else (
                split_by_parts(markdown) if split_by == "part" else split_by_sections(markdown) if split_by == "section" else split_by_chapters(markdown)
            )
            for chapter in legacy:
                chapter.kind = kind
                chapter.confidence = 0.35
                chapter.needs_review = True
                chapter.review_reasons = ["未能建立可靠目录树，使用回退切分。"]
            return SplitPlan(legacy, "legacy-fallback", True, reasons + ["未识别到足够的结构标题，使用大小或旧规则回退。"], toc_nodes, body_nodes, unmatched)
        reasons.append("未找到可用 PDF 书签或目录，按正文结构标题拆分。" if outline_nodes is None else "PDF 书签未能建立可靠目录，按正文结构标题拆分。")
        for body in body_targets:
            chapters.append(
                Chapter(
                    body.number,
                    body.title,
                    body.start,
                    len(markdown),
                    "",
                    node_id=body.node_id,
                    kind=kind,
                    logical_level=body.logical_level,
                    confidence=0.72,
                    needs_review=True,
                    review_reasons=["缺少可对齐目录，仅根据正文标题拆分。"],
                    page_start=body.page,
                )
            )
    chapters.sort(key=chapter_order_key)
    for index, chapter in enumerate(chapters):
        chapter.end = chapters[index + 1].start if index + 1 < len(chapters) else len(markdown)
        chapter.content = markdown[chapter.start:chapter.end].strip()
        chapter.line_start = line_number_at(markdown, chapter.start)
        chapter.line_end = line_number_at(markdown, max(chapter.start, chapter.end - 1))
        chapter.page_end = page_at(resolved_page_marks, max(chapter.start, chapter.end - 1))
        if chapter.end - chapter.start < 120:
            chapter.needs_review = True
            (chapter.review_reasons or []).append("章节正文过短，可能存在错误边界。")
    assigned_starts = {chapter.start for chapter in chapters}
    for node in body_targets:
        if node.start not in assigned_starts:
            owner = next((chapter for chapter in chapters if chapter.start < node.start < chapter.end), None)
            if owner:
                owner.needs_review = True
                (owner.review_reasons or []).append(f"章节内存在未分配标题：{node.number} {node.title}。")
    needs_review = bool(reasons or unmatched or any(chapter.needs_review for chapter in chapters))
    return SplitPlan(chapters, method, needs_review, reasons, toc_nodes, body_nodes, unmatched)


def split_chapters(markdown: str, split_by: str = "auto") -> list[Chapter]:
    """Compatibility wrapper for callers that only need chapter content."""
    return build_split_plan(markdown, split_by).chapters


def offset_at_line(markdown: str, line_number: int) -> int | None:
    if line_number < 1:
        return None
    if line_number == 1:
        return 0
    offset = 0
    for current, line in enumerate(markdown.splitlines(keepends=True), start=1):
        offset += len(line)
        if current == line_number - 1:
            return offset
    return None


def nearest_anchor_offset(markdown: str, anchor: str, preferred: int) -> int | None:
    if not anchor:
        return None
    matches: list[int] = []
    cursor = 0
    while True:
        found = markdown.find(anchor, cursor)
        if found < 0:
            break
        matches.append(found)
        cursor = found + max(1, len(anchor))
    if not matches:
        return None
    return line_start_at(markdown, min(matches, key=lambda item: abs(item - preferred)))


def load_overrides(path: Path | None) -> list[dict]:
    if not path or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid chapter overrides JSON: {path} ({exc})") from exc
    entries = data.get("overrides", data) if isinstance(data, dict) else data
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        raise RuntimeError("chapter overrides must be a JSON list or an object with an 'overrides' list.")
    return entries


def override_matches(chapter: Chapter, override: dict) -> bool:
    target = str(override.get("id") or override.get("node") or "").strip()
    display = f"{chapter.number} {chapter.title}".strip()
    if target and target in {chapter.node_id, display}:
        return True
    title = str(override.get("title") or "").strip()
    number = str(override.get("number") or "").strip()
    title_matches = normalize_match_text(title) in {normalize_match_text(chapter.title), normalize_match_text(display)}
    return bool(title and title_matches and (not number or normalize_number(number) == normalize_number(chapter.number)))


def apply_overrides(markdown: str, plan: SplitPlan, overrides: list[dict], page_marks: list[tuple[int, int]]) -> None:
    if not overrides:
        return
    kept: list[Chapter] = []
    for chapter in plan.chapters:
        override = next((item for item in overrides if override_matches(chapter, item)), None)
        if not override:
            kept.append(chapter)
            continue
        if bool(override.get("skip", False)):
            plan.review_reasons.append(f"人工覆盖跳过章节：{chapter.number} {chapter.title}。")
            continue
        new_start: int | None = None
        if override.get("startLine") is not None:
            try:
                new_start = offset_at_line(markdown, int(override["startLine"]))
            except (TypeError, ValueError):
                new_start = None
        if new_start is None and override.get("startAnchor"):
            new_start = nearest_anchor_offset(markdown, str(override["startAnchor"]), chapter.start)
        if new_start is None and override.get("startPage") is not None:
            try:
                target_page = int(override["startPage"])
                marks = [position for position, page in page_marks if page == target_page]
                if marks:
                    new_start = min(marks, key=lambda position: abs(position - chapter.start))
            except (TypeError, ValueError):
                pass
        if new_start is None and any(key in override for key in ("startLine", "startAnchor", "startPage")):
            chapter.needs_review = True
            (chapter.review_reasons or []).append("人工覆盖未能定位指定起点。")
        elif new_start is not None:
            chapter.start = new_start
            chapter.confidence = 1.0
            (chapter.review_reasons or []).append("已应用人工章节边界覆盖。")
        kept.append(chapter)
    plan.chapters = sorted(kept, key=lambda chapter: chapter.start)
    for index, chapter in enumerate(plan.chapters):
        chapter.end = plan.chapters[index + 1].start if index + 1 < len(plan.chapters) else len(markdown)
        chapter.content = markdown[chapter.start:chapter.end].strip()
        chapter.line_start = line_number_at(markdown, chapter.start)
        chapter.line_end = line_number_at(markdown, max(chapter.start, chapter.end - 1))
        chapter.page_start = page_at(page_marks, chapter.start)
        chapter.page_end = page_at(page_marks, max(chapter.start, chapter.end - 1))


def semantic_split_errors(plan: SplitPlan, outline_nodes: list[StructureNode] | None, split_by: str) -> list[str]:
    """Reject an output whose chapter tree no longer matches the PDF outline."""
    if not outline_nodes:
        return []
    expected_kind = target_kind(split_by, outline_nodes, plan.body_nodes)
    expected = nodes_for_kind(outline_nodes, expected_kind)
    actual_ids = [chapter.node_id for chapter in plan.chapters]
    expected_ids = [node.node_id for node in expected]
    errors: list[str] = []
    if len(actual_ids) != len(expected_ids):
        errors.append(f"PDF 书签要求 {len(expected_ids)} 个 {expected_kind} 节点，但仅定位到 {len(actual_ids)} 个。")
    missing = [node for node in expected if node.node_id not in set(actual_ids)]
    if missing:
        errors.append(f"未定位书签节点：{', '.join(f'{node.number} {node.title}'.strip() for node in missing[:5])}{' ...' if len(missing) > 5 else ''}")
    if any(right.start <= left.start for left, right in zip(plan.chapters, plan.chapters[1:])):
        errors.append("章节边界不是严格递增的 Markdown 位置。")
    return errors


def node_mapping(node: StructureNode) -> dict:
    return {
        "id": node.node_id,
        "number": node.number,
        "title": node.title,
        "kind": node.kind,
        "logicalLevel": node.logical_level,
        "line": node.line,
        "page": node.page,
        "tocPage": node.toc_page,
        "source": node.source,
    }


def split_mapping(plan: SplitPlan, source_vault_path: str, page_count: int) -> dict:
    return {
        "version": 3,
        "source": source_vault_path,
        "pageCount": page_count,
        "splitMethod": plan.method,
        "needsReview": plan.needs_review,
        "reviewReasons": plan.review_reasons,
        "chapters": [
            {
                "id": chapter.node_id,
                "number": chapter.number,
                "title": chapter.title,
                "kind": chapter.kind,
                "logicalLevel": chapter.logical_level,
                "sourceLineStart": chapter.line_start,
                "sourceLineEnd": chapter.line_end,
                "sourcePageStart": chapter.page_start,
                "sourcePageEnd": chapter.page_end,
                "confidence": round(chapter.confidence, 3),
                "needsReview": chapter.needs_review,
                "reviewReasons": chapter.review_reasons or [],
                "folder": chapter.output_folder,
                "imageCount": chapter.image_count,
                "missingImages": chapter.missing_images or [],
            }
            for chapter in plan.chapters
        ],
        "unmatchedTocNodes": [node_mapping(node) for node in plan.unmatched_toc_nodes],
        "tocNodes": [node_mapping(node) for node in plan.toc_nodes],
        "bodyNodes": [node_mapping(node) for node in plan.body_nodes],
    }


def review_markdown(base_name: str, plan: SplitPlan) -> str:
    lines = [f"# {base_name} - 待人工复核", ""]
    if not plan.needs_review:
        return "\n".join(lines + ["未发现需要人工复核的章节边界。", ""])
    lines += ["## 总体原因", ""]
    lines += [f"- {reason}" for reason in plan.review_reasons] or ["- 存在低置信度章节边界。"]
    if plan.unmatched_toc_nodes:
        lines += ["", "## 未匹配目录节点", ""]
        lines += [f"- {node.number} {node.title}" for node in plan.unmatched_toc_nodes]
    flagged = [chapter for chapter in plan.chapters if chapter.needs_review]
    if flagged:
        lines += ["", "## 章节边界", ""]
        for chapter in flagged:
            lines.append(f"### {chapter.number} {chapter.title}")
            lines.append("")
            lines.append(f"- 行号：{chapter.line_start}")
            lines.append(f"- 置信度：{chapter.confidence:.0%}")
            if chapter.page_start is not None:
                lines.append(f"- 起始页：{chapter.page_start}")
            lines += [f"- {reason}" for reason in (chapter.review_reasons or [])]
            lines.append("")
    lines += ["## 人工覆盖", "", "可在 `chapter-overrides.json` 中按 id/title 写入 `startAnchor`、`startLine`、`startPage` 或 `skip`，然后重新运行。", ""]
    return "\n".join(lines)


def line_number_at(markdown: str, index: int) -> int:
    return markdown.count("\n", 0, max(0, index)) + 1


def markdown_image_refs(markdown: str) -> list[str]:
    refs: list[str] = []
    for pattern in (r"!\[[^\]]*\]\(([^)]+)\)", r"!\[\[([^\]|#]+)(?:\|[^\]]*)?\]\]"):
        for match in re.finditer(pattern, markdown):
            ref = clean_image_ref(match.group(1))
            if ref and ref not in refs:
                refs.append(ref)
    return refs


def clean_image_ref(ref: str) -> str:
    ref = str(ref or "").strip().strip("<>")
    path = ref.split("#", 1)[0].split("?", 1)[0].strip()
    if not path or re.match(r"^(https?:|data:|#)", path, flags=re.I):
        return ""
    path = urllib.parse.unquote(path).replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def is_safe_relative_path(value: str) -> bool:
    if not value or "\x00" in value:
        return False
    normalized = value.replace("\\", "/")
    member = PureWindowsPath(normalized)
    return not (
        normalized.startswith("/")
        or member.is_absolute()
        or member.drive
        or ".." in member.parts
    )


def copy_chapter_images(markdown: str, source_dir: Path, image_dir: Path) -> tuple[str, int, list[str]]:
    refs = markdown_image_refs(markdown)
    mapping: dict[str, str] = {}
    missing: list[str] = []
    copied = 0
    source_root = source_dir.resolve()
    for ref in refs:
        if not is_safe_relative_path(ref):
            missing.append(ref)
            continue
        candidates = [source_root / ref, source_root / "images" / Path(ref).name]
        src = next(
            (
                candidate
                for candidate in candidates
                if candidate.resolve().is_relative_to(source_root) and candidate.exists() and candidate.is_file()
            ),
            None,
        )
        if not src:
            missing.append(ref)
            continue
        image_dir.mkdir(parents=True, exist_ok=True)
        dest_name = safe_name(src.name, "image")
        dest = image_dir / dest_name
        if dest.exists() and dest.read_bytes() != src.read_bytes():
            digest = hashlib.blake2b(ref.encode("utf-8"), digest_size=4).hexdigest()
            dest = image_dir / f"{src.stem}-{digest}{src.suffix}"
            dest_name = dest.name
        if not dest.exists():
            shutil.copy2(src, dest)
            copied += 1
        mapping[ref] = f"images/{dest_name}"

    def repl_md(match: re.Match[str]) -> str:
        alt, ref = match.group(1), match.group(2).strip()
        clean = clean_image_ref(ref)
        return f"![{alt}]({mapping.get(clean, ref)})"

    def repl_wiki(match: re.Match[str]) -> str:
        ref, suffix = match.group(1).strip(), match.group(2)
        clean = clean_image_ref(ref)
        return f"![[{mapping.get(clean, ref)}{suffix}]]"

    markdown = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", repl_md, markdown)
    markdown = re.sub(r"!\[\[([^\]|#]+)([^\]]*)\]\]", repl_wiki, markdown)
    return markdown, copied, missing


def quality_report(
    title: str,
    source_path: str,
    markdown: str,
    image_count: int,
    missing: list[str],
    needs_review: bool = False,
    review_reasons: list[str] | None = None,
) -> str:
    headings = len([line for line in markdown.splitlines() if re.match(r"^\s*#{1,6}\s+", line)])
    warnings = []
    if headings == 0:
        warnings.append("Markdown 内没有标题层级，后续复查章节边界。")
    if missing:
        warnings.append(f"有 {len(missing)} 个图片引用未找到源文件。")
    if needs_review:
        warnings.extend(review_reasons or ["章节边界置信度不足，需要人工复核。"])
    verdict = (
        "通过：已使用图文 Markdown / MinerU，并复制本地图片资源。"
        if not missing and not needs_review
        else (
            f"部分通过：有 {len(missing)} 个图片引用未复制成功。"
            if missing
            else "待复核：图文输出已生成，但章节边界需要人工确认。"
        )
    )
    lines = [
        f"# {title}",
        "",
        "## 质量结论",
        "",
        f"- 判定：{verdict}",
        "- 图文链路：已命中 MinerU 图文 Markdown 源。",
        "- Markdown 输出：已按章节输出 Markdown。",
        "",
        "## 解析来源",
        "",
        "- 模式：图文 Markdown / MinerU",
        f"- 来源：{source_path}",
        "- 章节数：1",
        f"- 标题数：{headings}",
        f"- 图片数：{image_count}",
        f"- 缺失图片：{len(missing)}",
        "",
    ]
    if warnings:
        lines += ["## 需要注意", ""]
        lines += [f"- {item}" for item in warnings]
        lines.append("")
    if missing:
        lines += ["## 缺失图片", ""]
        lines += [f"- {item}" for item in missing[:80]]
        if len(missing) > 80:
            lines.append(f"- ... 另 {len(missing) - 80} 项")
        lines.append("")
    return "\n".join(lines)


def chapter_markdown(
    base_name: str,
    source_file: str,
    source_vault_path: str,
    chapter: Chapter,
    index: int,
    total: int,
    generated: str,
    page_count: int,
) -> str:
    title = f"{chapter.number} {chapter.title}".strip()
    content = normalize_md(chapter.content)
    return "\n".join(
        [
            "---",
            'generatedBy: "mineru-textbook-to-md"',
            "artifact: structured-pdf-source",
            f"source: {quote_yaml(source_file)}",
            'sourceFormat: "markdown"',
            'parser: "MinerU"',
            'sourceMode: "rich-markdown"',
            f"chapterNumber: {quote_yaml(chapter.number)}",
            f"chapterTitle: {quote_yaml(chapter.title)}",
            f"chapterIndex: {index + 1}",
            f"chapterCount: {total}",
            f"chapterId: {quote_yaml(chapter.node_id)}",
            f"chapterKind: {quote_yaml(chapter.kind)}",
            f"splitConfidence: {chapter.confidence:.3f}",
            f"needsReview: {'true' if chapter.needs_review else 'false'}",
            f"pageCount: {page_count}",
            f"sourcePageStart: {chapter.page_start if chapter.page_start is not None else 'null'}",
            f"sourcePageEnd: {chapter.page_end if chapter.page_end is not None else 'null'}",
            f"imageCount: {chapter.image_count}",
            f"missingImageCount: {len(chapter.missing_images or [])}",
            f"generated: {quote_yaml(generated)}",
            "---",
            "",
            f"# {base_name} - {title}",
            "",
            "## 解析信息",
            "",
            f"- 来源文件：{source_file}",
            "- 解析方式：MinerU",
            "- 图文来源：是",
            f"- 章节位置：{index + 1}/{total}",
            f"- 拆分置信度：{chapter.confidence:.0%}",
            f"- 需要复核：{'是' if chapter.needs_review else '否'}",
            f"- 图片数量：{chapter.image_count}",
            f"- 缺失图片：{len(chapter.missing_images or [])}",
            f"- 字符数：{len(content)}",
            "",
            "## 原文",
            "",
            content,
            "",
        ]
    )


def split_preview(
    base_name: str,
    source_vault_path: str,
    plan: SplitPlan,
    generated: str,
    page_count: int,
) -> str:
    chapters = plan.chapters
    lines = [
        "---",
        'generatedBy: "mineru-textbook-to-md"',
        "artifact: structured-pdf-split-preview",
        f"source: {quote_yaml(source_vault_path)}",
        'sourceMode: "rich-markdown"',
        'parser: "MinerU"',
        f"pageCount: {page_count}",
        f"chapterCount: {len(chapters)}",
        f"splitMethod: {quote_yaml(plan.method)}",
        f"needsReview: {'true' if plan.needs_review else 'false'}",
        f"generated: {quote_yaml(generated)}",
        "---",
        "",
        f"# {base_name} - Markdown 章节拆分预览",
        "",
        "| # | 章节 | full.md 行号 | 页码 | 置信度 | 复核 | 字符数 | 图片 | 缺失图片 |",
        "|---:|---|---:|---:|---:|---|---:|---:|---:|",
    ]
    for index, chapter in enumerate(chapters, start=1):
        lines.append(
            f"| {index} | {chapter.number} {chapter.title} | "
            f"{chapter.line_start} | {chapter.page_start or '-'} | {chapter.confidence:.0%} | "
            f"{'是' if chapter.needs_review else '否'} | {len(chapter.content)} | "
            f"{chapter.image_count} | {len(chapter.missing_images or [])} |"
        )
    lines += [
        "",
        "## 说明",
        "",
        f"拆分方法：{plan.method}。",
        "详细边界、目录和正文对齐记录见 `00-章节映射.json`；需要人工确认的内容见 `00-待人工复核.md`。",
        "",
    ]
    return "\n".join(lines)


def total_report(base_name: str, source_vault_path: str, plan: SplitPlan) -> str:
    chapters = plan.chapters
    total_images = sum(ch.image_count for ch in chapters)
    missing = [item for ch in chapters for item in (ch.missing_images or [])]
    headings = sum(
        len([line for line in ch.content.splitlines() if re.match(r"^\s*#{1,6}\s+", line)])
        for ch in chapters
    )
    verdict = (
        "通过：已使用图文 Markdown / MinerU，图片引用可追踪。"
        if not missing and not plan.needs_review
        else (
            f"部分通过：有 {len(missing)} 个图片引用未复制成功。"
            if missing
            else "待复核：图文输出已生成，但章节边界需要人工确认。"
        )
    )
    lines = [
        f"# {base_name} - 文档解析质量总报告",
        "",
        "## 质量结论",
        "",
        f"- 判定：{verdict}",
        "- 图文链路：已命中 MinerU 图文 Markdown 源。",
        "- Markdown 输出：已按章节输出 Markdown。",
        "",
        "## 解析来源",
        "",
        "- 模式：图文 Markdown / MinerU",
        f"- 来源：{source_vault_path}",
        f"- 章节数：{len(chapters)}",
        f"- 标题数：{headings}",
        f"- 图片数：{total_images}",
        f"- 缺失图片：{len(missing)}",
        f"- 拆分方法：{plan.method}",
        f"- 待复核章节：{sum(ch.needs_review for ch in chapters)}",
        "",
    ]
    if missing:
        lines += ["## 缺失图片", ""]
        lines += [f"- {item}" for item in missing[:120]]
        if len(missing) > 120:
            lines.append(f"- ... 另 {len(missing) - 120} 项")
        lines.append("")
    if plan.needs_review:
        lines += ["## 需要人工复核", ""]
        lines += [f"- {reason}" for reason in plan.review_reasons] or ["- 详见 `00-待人工复核.md`。"]
        lines.append("")
    return "\n".join(lines)


def pdf_outline_markdown(base_name: str, nodes: list[StructureNode], page_count: int) -> str:
    lines = [
        f"# {base_name} - PDF 书签分级目录",
        "",
        f"> 来源：PDF 内置书签；共 {len(nodes)} 条，PDF 共 {page_count} 页。",
        "> 说明：书签作为目录骨架；标题层级依据书签结构及标题标记重建，括号内为 PDF 物理页码。",
        "",
    ]
    for node in nodes:
        level = min(6, max(1, node.logical_level))
        page = f"（PDF 第 {node.page} 页）" if node.page is not None else "（PDF 页码未指向具体页面）"
        lines.extend([f"{'#' * level} {node.title}{page}", ""])
    return "\n".join(lines)


def archive_generated_artifacts(output_dir: Path) -> None:
    """Archive prior generated chapter/report files without moving the PDF or raw parse."""
    candidates = []
    for child in output_dir.iterdir() if output_dir.exists() else []:
        is_report = child.name.startswith("00-") and child.name not in {"00-MinerU原始分块", "00-MinerU分块计划.json"}
        is_chapter = child.is_dir() and (
            (child / "01-原文解析.md").exists()
            or (child / "01-教材原文解析.md").exists()
            or any(grandchild.is_file() and grandchild.name in {"01-原文解析.md", "01-教材原文解析.md"} for grandchild in child.glob("*/01-*.md"))
        )
        if is_report or is_chapter:
            candidates.append(child)
    if not candidates:
        return
    trash = output_dir / ".trash" / dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    trash.mkdir(parents=True, exist_ok=True)
    for child in candidates:
        child.rename(trash / child.name)
    log(f"Archived {len(candidates)} generated artifact(s) to .trash/{trash.name}")


def copy_source_pdf(pdf_path: Path, output_dir: Path, force: bool = False) -> None:
    """Keep a non-destructive source copy beside all generated artifacts."""
    target = output_dir / pdf_path.name
    if target.resolve() == pdf_path.resolve():
        return
    if target.exists():
        same_content = (
            target.is_file()
            and target.stat().st_size == pdf_path.stat().st_size
            and sha256_file(target) == sha256_file(pdf_path)
        )
        if same_content:
            return
        if not force:
            raise RuntimeError(
                f"Destination PDF already exists with different content: {target}; pass --force to replace it."
            )
    shutil.copy2(pdf_path, target)


def chapter_part_names(outline_nodes: list[StructureNode] | None) -> dict[str, tuple[int, str]]:
    """Map each bookmark chapter to the active part/篇 in source order."""
    if not outline_nodes:
        return {}
    result: dict[str, tuple[int, str]] = {}
    active_part: tuple[int, str] | None = None
    part_index = 0
    for node in outline_nodes:
        if node.kind == "part":
            part_index += 1
            active_part = (part_index, safe_name(f"{node.number} {node.title}".strip()))
        elif node.kind == "chapter" and active_part:
            result[node.node_id] = active_part
    return result


def write_chapter_outputs(
    markdown: str,
    pdf_path: Path,
    base_name: str,
    source_dir: Path,
    source_vault_path: str,
    output_dir: Path,
    page_count: int,
    split_by: str = "auto",
    plan: SplitPlan | None = None,
    outline_nodes: list[StructureNode] | None = None,
    archive_output: bool = False,
) -> list[Chapter]:
    if output_dir.exists() and archive_output:
        archive_generated_artifacts(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    plan = plan or build_split_plan(markdown, split_by, outline_nodes=outline_nodes)
    chapters = plan.chapters
    generated = dt.datetime.now().replace(microsecond=0).isoformat()
    log(f"Detected {len(chapters)} chapter(s). Writing chapter Markdown.")
    part_names = chapter_part_names(outline_nodes)
    used_folder_names: dict[Path, set[str]] = {}
    chapter_positions: dict[Path, int] = {}
    for index, chapter in enumerate(chapters):
        chapter.line_start = line_number_at(markdown, chapter.start)
        chapter.line_end = line_number_at(markdown, max(chapter.start, chapter.end - 1))
        part_index, part_name = part_names.get(chapter.node_id, (99, "未归类章节"))
        parent = output_dir / f"{part_index:02d}-{part_name}"
        parent.mkdir(parents=True, exist_ok=True)
        names_in_parent = used_folder_names.setdefault(parent, set())
        chapter_positions[parent] = chapter_positions.get(parent, 0) + 1
        folder_name = safe_name(f"{chapter_positions[parent]:02d}-{chapter.number} {chapter.title}".strip())
        unique_name = folder_name
        duplicate = 2
        while unique_name in names_in_parent:
            unique_name = f"{folder_name} ({duplicate})"
            duplicate += 1
        names_in_parent.add(unique_name)
        folder = parent / unique_name
        folder.mkdir(parents=True, exist_ok=True)
        chapter.output_folder = folder.relative_to(output_dir).as_posix()
        image_dir = folder / "images"
        rewritten, copied, missing = copy_chapter_images(chapter.content, source_dir, image_dir)
        chapter.content = normalize_headings(rewritten)
        chapter.image_count = copied
        chapter.missing_images = missing
        (folder / "01-原文解析.md").write_text(
            chapter_markdown(
                base_name=base_name,
                source_file=pdf_path.name,
                source_vault_path=source_vault_path,
                chapter=chapter,
                index=index,
                total=len(chapters),
                generated=generated,
                page_count=page_count,
            ),
            encoding="utf-8",
        )
        (folder / "02-解析质量报告.md").write_text(
            quality_report(
                title=f"{base_name} - {chapter.number} {chapter.title}",
                source_path=source_vault_path,
                markdown=chapter.content,
                image_count=chapter.image_count,
                missing=chapter.missing_images or [],
                needs_review=chapter.needs_review,
                review_reasons=chapter.review_reasons,
            ),
            encoding="utf-8",
        )
    (output_dir / "00-章节拆分预览.md").write_text(
        split_preview(base_name, source_vault_path, plan, generated, page_count),
        encoding="utf-8",
    )
    if outline_nodes:
        (output_dir / "00-PDF书签分级目录.md").write_text(
            pdf_outline_markdown(base_name, outline_nodes, page_count),
            encoding="utf-8",
        )
    (output_dir / "00-解析质量总报告.md").write_text(
        total_report(base_name, source_vault_path, plan),
        encoding="utf-8",
    )
    (output_dir / "00-章节映射.json").write_text(
        json.dumps(split_mapping(plan, source_vault_path, page_count), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "00-待人工复核.md").write_text(
        review_markdown(base_name, plan),
        encoding="utf-8",
    )
    return chapters


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="mineru-textbook-to-md",
        description="Convert a structured PDF or existing MinerU Markdown into an ordered, reviewable Markdown package.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--pdf", type=Path, help="PDF file to process with MinerU (required unless --from-full-md is given).")
    parser.add_argument(
        "--from-full-md",
        type=Path,
        default=None,
        help="Skip MinerU; split an existing full.md in the given directory.",
    )
    parser.add_argument("--base-name", default="", help="Book folder name; defaults to the PDF stem or full.md parent name.")
    parser.add_argument(
        "--mode",
        choices=("source-folder",),
        default="source-folder",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output parent directory; a book-named folder is created inside it unless it already has that name.",
    )
    parser.add_argument(
        "--api-token-env",
        default="MINERU_OFFICIAL_API_TOKEN",
        help="Environment variable containing the MinerU API token.",
    )
    parser.add_argument(
        "--direct-download",
        action="store_true",
        help="Bypass system proxies only for MinerU result downloads (use when the CDN cannot be reached through a proxy).",
    )
    parser.add_argument("--method", choices=("auto", "txt", "ocr"), default="auto", help="MinerU parsing method.")
    parser.add_argument(
        "--model-version", choices=("vlm", "pipeline"), default="vlm", help="MinerU model family."
    )
    parser.add_argument(
        "--page-range-size",
        type=int,
        default=200,
        help="Maximum pages per MinerU upload chunk (clamped to 1-200).",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Read PDF bookmarks and write 00-MinerU分块计划.json without uploading or splitting Markdown.",
    )
    parser.add_argument(
        "--split-by",
        choices=("auto", "part", "section", "chapter"),
        default="auto",
        help="Chapter granularity: auto detects structure; part = one per 部/篇; section = one per 节; chapter = one per 第X章 (for books without 节 layer).",
    )
    parser.add_argument("--no-formula", action="store_true", help="Disable MinerU formula recognition.")
    parser.add_argument("--no-table", action="store_true", help="Disable MinerU table recognition.")
    parser.add_argument("--page-count", type=int, default=None, help="Source PDF page count when splitting an existing full.md.")
    parser.add_argument("--layout-json", type=Path, default=None, help="Optional MinerU layout JSON used to recover page hints for chapter mapping.")
    parser.add_argument("--overrides", type=Path, default=None, help="Optional chapter-overrides.json path for manual boundary corrections.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace conflicting source copies and rebuild MinerU-owned cache artifacts.",
    )
    parser.add_argument("--archive-output", action="store_true", help="Move the existing split output to .trash before writing a clean rebuild.")
    parser.add_argument(
        "--allow-unverified-split",
        action="store_true",
        help="Allow writing when PDF-bookmark semantic validation fails (not recommended).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan the chapter split and print the list without writing any files (no .trash move, no image copy).",
    )
    parser.add_argument("--debug", action="store_true", help="Show a traceback when a command fails.")
    args = parser.parse_args()

    from_full_input = args.from_full_md.resolve() if args.from_full_md else None
    if args.plan_only and from_full_input:
        raise RuntimeError("--plan-only cannot be combined with --from-full-md.")
    if args.plan_only and args.dry_run:
        raise RuntimeError("--plan-only cannot be combined with --dry-run because it writes a plan file.")
    if args.page_count is not None and args.page_count < 0:
        raise RuntimeError("--page-count must be zero or greater.")
    if not args.pdf and not from_full_input:
        raise RuntimeError("One of --pdf or --from-full-md is required.")
    if from_full_input:
        full_md_input = from_full_input if from_full_input.is_file() else from_full_input / "full.md"
        if not full_md_input.exists() or not full_md_input.is_file():
            raise RuntimeError(f"full.md not found at {from_full_input}")
        raw_source_dir = full_md_input.parent
    else:
        full_md_input = None
        raw_source_dir = None

    pdf_path = args.pdf.resolve() if args.pdf else None
    base_name = args.base_name or (raw_source_dir.name if raw_source_dir else pdf_path.stem)

    # Keep every generated artifact in one book-named folder. The original PDF
    # is copied there, while a source PDF outside that folder remains untouched.
    if args.output:
        output_parent = args.output.resolve()
    elif pdf_path:
        output_parent = pdf_path.parent
    else:
        output_parent = raw_source_dir
    book_folder_name = safe_name(base_name)
    source_base = (
        output_parent
        if output_parent.name.casefold() == book_folder_name.casefold()
        else output_parent / book_folder_name
    )
    chapter_output = source_base
    settings = None

    if full_md_input:
        # --- direct split from existing full.md (no MinerU) ---
        full_md_path = full_md_input
        if pdf_path and not args.dry_run:
            source_base.mkdir(parents=True, exist_ok=True)
            copy_source_pdf(pdf_path, source_base, args.force)
        if full_md_path.parent != source_base and not args.dry_run:
            source_base.mkdir(parents=True, exist_ok=True)
            target_full_md = source_base / "full.md"
            full_md_path = copy_full_md_input(full_md_path, target_full_md, args.force)
        markdown = normalize_md(full_md_path.read_text(encoding="utf-8", errors="replace"))
        repaired = repair_toc_body_separation(markdown)
        if repaired != markdown:
            if not args.dry_run:
                full_md_path.write_text(repaired + "\n", encoding="utf-8")
            log("Repaired TOC/body separation in memory.")
            markdown = repaired
        page_count = (
            int(args.page_count)
            if args.page_count is not None
            else len(PdfReader(str(pdf_path)).pages) if pdf_path else 0
        )
        try:
            source_vault_path = str(full_md_path.relative_to(source_base)).replace("\\", "/")
        except ValueError:
            source_vault_path = str(full_md_path)
        source_dir = raw_source_dir
        settings = {"mode": "official"}  # dummy, won't be used for MinerU API
    else:
        # --- normal MinerU pipeline ---
        settings = {
            "enabled": True,
            "mode": "official",
            "method": args.method,
            "modelVersion": args.model_version,
            "formula": not args.no_formula,
            "table": not args.no_table,
            "pageRangeSize": max(1, min(200, int(args.page_range_size or 200))),
            "apiTokenEnv": str(args.api_token_env or "").strip(),
        }
        if settings["mode"] != "official":
            raise RuntimeError(f"Unsupported MinerU API mode: {settings['mode']}.")
        page_count = len(PdfReader(str(pdf_path)).pages)
        cached_full = source_base / "full.md"
        if args.dry_run and (args.force or not cached_full.exists() or cached_full.stat().st_size == 0):
            raise RuntimeError("--dry-run requires an existing full.md; it never uploads a PDF. Run once without --dry-run first.")
        if not args.dry_run:
            source_base.mkdir(parents=True, exist_ok=True)
            copy_source_pdf(pdf_path, source_base, args.force)
        try:
            preflight_outline = extract_pdf_outline(pdf_path)
            if preflight_outline:
                log(f"Read {len(preflight_outline)} PDF bookmark(s) before MinerU upload.")
            else:
                log("No usable PDF bookmarks found before upload; using fixed page ranges.")
        except Exception as exc:
            preflight_outline = None
            log(f"Could not read PDF bookmarks before upload; using fixed page ranges: {exc}")
        if args.plan_only:
            ranges, chunk_plan = semantic_page_ranges(page_count, settings["pageRangeSize"], preflight_outline)
            source_base.mkdir(parents=True, exist_ok=True)
            (source_base / "00-MinerU分块计划.json").write_text(
                json.dumps({"pageCount": page_count, "ranges": ranges, **chunk_plan}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            log(f"Wrote semantic upload plan: {source_base / '00-MinerU分块计划.json'}")
            return 0
        full_md_path = run_mineru_official(
            pdf_path,
            base_name,
            source_base,
            settings,
            page_count,
            outline_nodes=preflight_outline,
            force=args.force,
            direct_download=args.direct_download,
        )
        markdown = normalize_md(full_md_path.read_text(encoding="utf-8", errors="replace"))
        repaired = repair_toc_body_separation(markdown)
        if repaired != markdown:
            markdown = repaired
            log("Repaired TOC/body separation before splitting.")
        if not args.dry_run:
            full_md_path.write_text(markdown + "\n", encoding="utf-8")
        source_vault_path = str(full_md_path.relative_to(source_base)).replace("\\", "/")
        source_dir = full_md_path.parent
    layout_json = args.layout_json.resolve() if args.layout_json else None
    outline_nodes: list[StructureNode] | None = locals().get("preflight_outline")
    if pdf_path:
        try:
            outline_nodes = extract_pdf_outline(pdf_path)
            if outline_nodes:
                log(f"Loaded {len(outline_nodes)} PDF bookmark(s) as the TOC framework.")
            else:
                log("No usable PDF bookmarks found; falling back to Markdown TOC detection.")
        except Exception as exc:
            log(f"Could not read PDF bookmarks; falling back to Markdown TOC detection: {exc}")
    raw_chunk_dir = source_dir / "00-MinerU原始分块"
    chunk_page_marks = collect_chunk_page_marks(markdown, raw_chunk_dir)
    if chunk_page_marks:
        log(f"Recovered {len(chunk_page_marks)} source-page anchor(s) from retained MinerU chunks.")
    plan = build_split_plan(
        markdown,
        args.split_by,
        layout_json,
        outline_nodes=outline_nodes,
        page_marks=chunk_page_marks,
    )
    override_path = args.overrides.resolve() if args.overrides else source_dir / "chapter-overrides.json"
    page_marks = collect_page_marks(markdown, layout_json, chunk_page_marks)
    apply_overrides(markdown, plan, load_overrides(override_path), page_marks)
    plan.needs_review = bool(plan.review_reasons or plan.unmatched_toc_nodes or any(chapter.needs_review for chapter in plan.chapters))
    semantic_errors = semantic_split_errors(plan, outline_nodes, args.split_by)
    if semantic_errors:
        plan.needs_review = True
        plan.review_reasons.extend(semantic_errors)
        if not args.allow_unverified_split and not args.dry_run:
            raise RuntimeError(
                "Semantic split validation failed; refusing to write chapter folders. "
                "Use --allow-unverified-split only for a deliberate manual repair. "
                + " ".join(semantic_errors)
            )
    if args.dry_run:
        width = max(2, len(str(len(plan.chapters))))
        log(f"[DRY-RUN] Would produce {len(plan.chapters)} chapter(s) with {sum(ch.needs_review for ch in plan.chapters)} review flag(s):")
        for index, chapter in enumerate(plan.chapters, start=1):
            log(f"  {index:0{width}d} | {chapter.number} {chapter.title} | line {chapter.line_start} | confidence {chapter.confidence:.0%}")
        for reason in plan.review_reasons:
            log(f"[REVIEW] {reason}")
        log("[DRY-RUN] No files were written or moved.")
        return 0
    chapters = write_chapter_outputs(
        markdown=markdown,
        pdf_path=pdf_path or full_md_path,
        base_name=base_name,
        source_dir=source_dir,
        source_vault_path=source_vault_path,
        output_dir=chapter_output,
        page_count=page_count,
        split_by=args.split_by,
        plan=plan,
        outline_nodes=outline_nodes,
        archive_output=args.archive_output,
    )
    log(f"Done: {chapter_output}")
    log(f"Chapters: {len(chapters)}")
    log(f"Images copied: {sum(ch.image_count for ch in chapters)}")
    log(f"Missing images: {sum(len(ch.missing_images or []) for ch in chapters)}")
    return 0


def run() -> int:
    try:
        return main()
    except KeyboardInterrupt:
        log("Cancelled by user.")
        return 130
    except Exception as exc:  # noqa: BLE001
        if "--debug" in sys.argv:
            raise
        log(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
