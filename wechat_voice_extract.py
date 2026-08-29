#!/usr/bin/env python3
"""Windows 微信收藏中的笔记语音恢复工具（在 WSL Ubuntu 中运行）。

日常用法：先在 Windows 微信中完整播放收藏中的笔记语音，再运行：

    python3 wechat_voice_extract.py

脚本只处理当前电脑中用户本人已播放、已加载到微信进程内存的语音。
Full dump 可能包含聊天内容、登录状态等敏感信息；成功时默认删除，失败时保留以便重试。
"""

from __future__ import annotations

import struct
import re
import hashlib
import argparse
import base64
import json
import mmap
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Protocol


class Buffer(Protocol):
    def __getitem__(self, key): ...
    def __len__(self) -> int: ...


@dataclass(frozen=True)
class MemoryRange:
    virtual_address: int
    size: int
    file_offset: int


@dataclass(frozen=True)
class VoiceMetadata:
    duration_ms: int
    full_size: int
    full_md5: str
    head256_md5: str | None
    data_path: str
    data_format: str = "speex"
    data_id: str | None = None
    source_offset: int = 0
    account_path_match: bool = False


@dataclass(frozen=True)
class VoiceMatch:
    metadata: VoiceMetadata
    file_offset: int
    virtual_address: int
    head_bytes: bytes


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    parent_pid: int
    working_set: int
    has_window: bool
    window_title: str
    loads_wxplayer: bool


SILK_MAGICS = (b"\x02#!SILK_V3", b"#!SILK_V3")
# 请把此处改成你自己的微信文件存储目录，或传入 --account-root；不能照抄示例。
DEFAULT_ACCOUNT_ROOT = r"E:\微信文件\xwechat_files\YOUR_WECHAT_ID"
DEFAULT_DECODER = "~/wechat-speex-declib/bin/speex_decode"
DEFAULT_SILK_DECODER = "~/.local/bin/silk_v3_decoder"
HEAD_CACHE = "~/.local/share/wechat_voice_extract/speex_head256.bin"


class ExtractError(RuntimeError):
    """A recoverable extraction failure with a user-facing message."""


def _u32(buffer: Buffer, offset: int) -> int:
    return struct.unpack_from("<I", buffer, offset)[0]


def _u64(buffer: Buffer, offset: int) -> int:
    return struct.unpack_from("<Q", buffer, offset)[0]


def parse_memory64_ranges(buffer: Buffer) -> list[MemoryRange]:
    if len(buffer) < 32 or buffer[0:4] != b"MDMP":
        raise ExtractError("文件不是有效的 Windows Minidump。")

    stream_count = _u32(buffer, 8)
    directory_rva = _u32(buffer, 12)
    if directory_rva + stream_count * 12 > len(buffer):
        raise ExtractError("Minidump 的流目录已损坏或不完整。")

    memory64_rva = None
    for index in range(stream_count):
        entry = directory_rva + index * 12
        stream_type, _data_size, rva = struct.unpack_from("<III", buffer, entry)
        if stream_type == 9:  # Memory64ListStream
            memory64_rva = rva
            break

    if memory64_rva is None or memory64_rva + 16 > len(buffer):
        raise ExtractError("Minidump 中没有 Memory64ListStream；请确认创建的是 full dump。")

    range_count = _u64(buffer, memory64_rva)
    base_rva = _u64(buffer, memory64_rva + 8)
    descriptor_start = memory64_rva + 16
    if range_count > 10_000_000 or descriptor_start + range_count * 16 > len(buffer):
        raise ExtractError("Memory64ListStream 的范围表已损坏。")

    result: list[MemoryRange] = []
    file_offset = base_rva
    for index in range(range_count):
        descriptor = descriptor_start + index * 16
        virtual_address = _u64(buffer, descriptor)
        size = _u64(buffer, descriptor + 8)
        if file_offset + size > len(buffer):
            raise ExtractError("Minidump 的内存数据不完整。")
        result.append(MemoryRange(virtual_address, size, file_offset))
        file_offset += size
    return result


_VOICE_RECORD = re.compile(
    r"datafmt\s*[:=]\s*(?P<format>speex|silk)\b"
    r".{0,768}?duration\s*[:=]\s*(?P<duration>[0-9]{2,9})"
    r".{0,1536}?(?<![A-Za-z0-9_])fullsize\s*[:=]\s*(?P<size>[0-9]{2,12})"
    r".{0,768}?(?<![A-Za-z0-9_])fullmd5\s*[:=]\s*(?P<fullmd5>[0-9a-fA-F]{32})"
    r".{0,4096}?datapath\s*[:=]\s*(?P<path>[A-Za-z]:[^\r\n\x00]{1,1500}?\.(?:speex|silk)_temp)",
    re.IGNORECASE | re.DOTALL,
)

_XML_DATAITEM = re.compile(
    r"<dataitem\b(?P<attrs>[^>]*)>(?P<body>.*?)</dataitem>",
    re.IGNORECASE | re.DOTALL,
)


def _xml_tag(body: str, name: str) -> str | None:
    match = re.search(
        rf"<{name}>\s*([^<]*?)\s*</{name}>",
        body,
        re.IGNORECASE | re.DOTALL,
    )
    return match.group(1).strip() if match else None


def _decode_windows(raw: bytes) -> list[str]:
    texts = [raw.decode("utf-8", errors="ignore")]
    # Strings in the dump can start on either UTF-16LE byte alignment.
    texts.extend(raw[offset:].decode("utf-16le", errors="ignore") for offset in (0, 1))
    return [text.replace("\x00", "") for text in texts]


def _collapse_backslashes(path: str) -> str:
    while "\\\\" in path:
        path = path.replace("\\\\", "\\")
    return path.strip()


def extract_voice_metadata(buffer: Buffer, account_root: str) -> list[VoiceMetadata]:
    """Extract de-duplicated WeChat Speex/Silk records without crossing dataitems."""
    anchors = (b"datafmt", "datafmt".encode("utf-16le"))
    anchor_offsets: set[int] = set()
    for needle in anchors:
        position = 0
        while True:
            position = buffer.find(needle, position)  # type: ignore[attr-defined]
            if position < 0:
                break
            anchor_offsets.add(position)
            position += max(1, len(needle))

    # A tiny test buffer or unusual dump may not expose a searchable method.
    if not anchor_offsets and len(buffer) <= 8 * 1024 * 1024:
        anchor_offsets.add(0)

    root = _collapse_backslashes(account_root).casefold().rstrip("\\/")
    unique: dict[tuple[int, str, str], VoiceMetadata] = {}
    heads_by_data_id: dict[str, str] = {}
    heads_by_full_md5: dict[str, str] = {}
    for anchor in sorted(anchor_offsets):
        window_start = max(0, anchor - 1024)
        window_end = min(len(buffer), anchor + 8192)
        raw = bytes(buffer[window_start:window_end])
        for text in _decode_windows(raw):
            # XML entries are bounded by one <dataitem>, preventing a note HTML
            # item's MD5 from being paired with the following voice item.
            for xml_item in _XML_DATAITEM.finditer(text):
                attrs = xml_item.group("attrs")
                body = xml_item.group("body")
                data_format = (_xml_tag(body, "datafmt") or "").casefold()
                if data_format not in {"speex", "silk"}:
                    continue
                full_md5 = (_xml_tag(body, "fullmd5") or "").casefold()
                head_md5 = (_xml_tag(body, "head256md5") or "").casefold()
                data_id_match = re.search(
                    r"\bdataid=[\"']([0-9a-fA-F]{32})[\"']",
                    attrs,
                    re.IGNORECASE,
                )
                if re.fullmatch(r"[0-9a-f]{32}", head_md5):
                    if re.fullmatch(r"[0-9a-f]{32}", full_md5):
                        heads_by_full_md5[full_md5] = head_md5
                    if data_id_match:
                        heads_by_data_id[data_id_match.group(1).casefold()] = head_md5

            for match in _VOICE_RECORD.finditer(text):
                full_size = int(match.group("size"))
                duration = int(match.group("duration"))
                if full_size < 60 or duration <= 0:
                    continue
                path = _collapse_backslashes(match.group("path"))
                context = text[max(0, match.start() - 768): min(len(text), match.end() + 768)]
                head_match = re.search(
                    r"(?<![A-Za-z0-9_])head256md5\s*[:=]\s*([0-9a-fA-F]{32})",
                    context,
                    re.IGNORECASE,
                )
                data_id_match = re.search(
                    r"(?<![A-Za-z0-9_])dataid\s*[:=]\s*([0-9a-fA-F]{32})",
                    context,
                    re.IGNORECASE,
                )
                item = VoiceMetadata(
                    duration_ms=duration,
                    full_size=full_size,
                    full_md5=match.group("fullmd5").lower(),
                    head256_md5=head_match.group(1).lower() if head_match else None,
                    data_path=path,
                    data_format=match.group("format").casefold(),
                    data_id=data_id_match.group(1).lower() if data_id_match else None,
                    source_offset=window_start,
                    account_path_match=path.casefold().startswith(root) if root else True,
                )
                key = (item.full_size, item.full_md5, item.data_path.casefold())
                previous = unique.get(key)
                if previous is None or (item.head256_md5 and not previous.head256_md5):
                    unique[key] = item

    result = []
    for item in unique.values():
        head_md5 = item.head256_md5
        if head_md5 is None and item.data_id:
            head_md5 = heads_by_data_id.get(item.data_id)
        if head_md5 is None:
            head_md5 = heads_by_full_md5.get(item.full_md5)
        result.append(replace(item, head256_md5=head_md5))
    return result


def merge_contiguous_ranges(ranges: list[MemoryRange]) -> list[MemoryRange]:
    merged: list[MemoryRange] = []
    for item in ranges:
        if merged:
            previous = merged[-1]
            if (
                previous.virtual_address + previous.size == item.virtual_address
                and previous.file_offset + previous.size == item.file_offset
            ):
                merged[-1] = MemoryRange(
                    previous.virtual_address,
                    previous.size + item.size,
                    previous.file_offset,
                )
                continue
        merged.append(item)
    return merged


def _match_at_offset(
    buffer: Buffer,
    merged_ranges: list[MemoryRange],
    position: int,
    metadata_items: list[VoiceMetadata],
) -> list[VoiceMatch]:
    containing = next(
        (
            item
            for item in merged_ranges
            if item.file_offset <= position < item.file_offset + item.size
        ),
        None,
    )
    if containing is None:
        return []
    remaining = containing.file_offset + containing.size - position
    head = bytes(buffer[position: position + 256])
    head_md5 = hashlib.md5(head).hexdigest()
    results: list[VoiceMatch] = []
    for metadata in metadata_items:
        if metadata.head256_md5 and metadata.head256_md5 != head_md5:
            continue
        if metadata.full_size > remaining:
            continue
        payload = buffer[position: position + metadata.full_size]
        if hashlib.md5(payload).hexdigest() != metadata.full_md5:
            continue
        virtual_address = containing.virtual_address + (position - containing.file_offset)
        results.append(VoiceMatch(metadata, position, virtual_address, head))
    return results


def find_voice_matches(
    buffer: Buffer,
    ranges: list[MemoryRange],
    metadata_items: list[VoiceMetadata],
    known_head: bytes | None = None,
    extra_head_md5s: set[str] | None = None,
    bootstrap_window: int = 64 * 1024,
) -> list[VoiceMatch]:
    """Find byte-perfect voice buffers, using a cached head or a bounded heap scan."""
    merged = merge_contiguous_ranges(ranges)
    expected_heads = {
        item.head256_md5 for item in metadata_items if item.head256_md5 is not None
    }
    expected_heads.update(extra_head_md5s or set())
    positions: set[int] = set()
    used_known_head = False
    if known_head is not None:
        if len(known_head) != 256:
            known_head = None
        elif hashlib.md5(known_head).hexdigest() not in expected_heads:
            known_head = None

    if known_head is not None:
        used_known_head = True
        for memory_range in merged:
            start = memory_range.file_offset
            end = start + memory_range.size
            position = buffer.find(known_head, start, end)  # type: ignore[attr-defined]
            while position >= 0:
                positions.add(position)
                position = buffer.find(known_head, position + 1, end)  # type: ignore[attr-defined]
    else:
        # Silk has a stable file signature. Search each complete memory range
        # rather than assuming the payload is near its start or 16-byte aligned.
        # The MD5 checks below still reject unrelated Silk buffers.
        if any(item.data_format == "silk" for item in metadata_items):
            for memory_range in merged:
                start = memory_range.file_offset
                end = start + memory_range.size
                for magic in SILK_MAGICS:
                    position = buffer.find(magic, start, end)  # type: ignore[attr-defined]
                    while position >= 0:
                        positions.add(position)
                        position = buffer.find(magic, position + 1, end)  # type: ignore[attr-defined]

        # Windows heap payloads observed here begin 0x80 bytes after a
        # Memory64 descriptor boundary. Scanning the first 64 KiB of every
        # range at 16-byte heap alignment finds that payload without trying
        # to hash every possible byte of a multi-gigabyte dump.
        for memory_range in ranges:
            start = memory_range.file_offset
            available = min(memory_range.size, bootstrap_window)
            stop = start + available - 255
            for position in range(start, max(start, stop), 16):
                digest = hashlib.md5(buffer[position: position + 256]).hexdigest()
                if digest in expected_heads:
                    positions.add(position)

    matches: list[VoiceMatch] = []
    seen: set[tuple[str, int]] = set()
    for position in positions:
        for match in _match_at_offset(buffer, merged, position, metadata_items):
            key = (match.metadata.full_md5, match.file_offset)
            if key not in seen:
                seen.add(key)
                matches.append(match)
    if not matches and used_known_head:
        return find_voice_matches(
            buffer,
            ranges,
            metadata_items,
            known_head=None,
            extra_head_md5s=extra_head_md5s,
            bootstrap_window=bootstrap_window,
        )
    return matches


def choose_main_weixin(processes: list[ProcessInfo]) -> ProcessInfo:
    if not processes:
        raise ExtractError("没有发现正在运行的 Weixin.exe。请先打开 Windows 微信。")
    pids = {item.pid for item in processes}

    def score(item: ProcessInfo) -> tuple[int, int]:
        value = 0
        if item.has_window:
            value += 1_000_000
        if item.window_title.strip():
            value += 500_000
        if item.parent_pid not in pids:
            value += 100_000
        if not item.loads_wxplayer:
            value += 50_000
        return value, item.working_set

    return max(processes, key=score)


def _powershell(script: str, timeout: int = 120) -> str:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            check=False,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ExtractError("WSL 无法调用 powershell.exe；请确认 Windows 互操作功能没有被关闭。") from exc
    except subprocess.TimeoutExpired as exc:
        raise ExtractError("Windows PowerShell 操作超时。") from exc
    stdout = result.stdout.decode("utf-8", errors="replace").strip()
    stderr = result.stderr.decode("utf-8", errors="replace").strip()
    if result.returncode != 0:
        detail = stderr or stdout or f"退出代码 {result.returncode}"
        raise ExtractError(f"Windows PowerShell 操作失败：{detail}")
    return stdout


def discover_weixin_processes() -> list[ProcessInfo]:
    script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$items = @()
Get-CimInstance Win32_Process -Filter "Name = 'Weixin.exe'" | ForEach-Object {
    $cim = $_
    $p = Get-Process -Id $cim.ProcessId -ErrorAction Stop
    $loadsPlayer = $false
    try {
        $loadsPlayer = @($p.Modules | Where-Object { $_.ModuleName -ieq 'wxplayer.dll' }).Count -gt 0
    } catch {}
    $items += [pscustomobject]@{
        pid = [int]$cim.ProcessId
        parent_pid = [int]$cim.ParentProcessId
        working_set = [int64]$p.WorkingSet64
        has_window = ([int64]$p.MainWindowHandle -ne 0)
        window_title = [string]$p.MainWindowTitle
        loads_wxplayer = [bool]$loadsPlayer
    }
}
ConvertTo-Json -InputObject @($items) -Compress
"""
    raw = _powershell(script)
    if not raw:
        return []
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ExtractError("无法读取 Windows 微信进程列表。") from exc
    if isinstance(values, dict):
        values = [values]
    return [
        ProcessInfo(
            pid=int(item["pid"]),
            parent_pid=int(item["parent_pid"]),
            working_set=int(item["working_set"]),
            has_window=bool(item["has_window"]),
            window_title=str(item.get("window_title") or ""),
            loads_wxplayer=bool(item["loads_wxplayer"]),
        )
        for item in values
    ]


def windows_to_wsl_path(windows_path: str) -> Path:
    try:
        result = subprocess.run(
            ["wslpath", "-u", windows_path],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise ExtractError(f"无法把 Windows 路径转换为 WSL 路径：{windows_path}") from exc
    return Path(result.stdout.strip())


def build_full_dump_batch(pid: int, windows_path: str, identity: str) -> str:
    """Build the cmd.exe form proven to preserve comsvcs' trailing full flag."""
    if pid <= 0 or not re.fullmatch(r"C:\\Dump\\[A-Za-z0-9_.-]+\.dmp", windows_path):
        raise ExtractError("full dump 的 PID 或目标路径不安全。")
    if not identity or any(character in identity for character in '\r\n"&|<>^%'):
        raise ExtractError("Windows 当前账户名称包含不支持的命令字符。")
    return "\r\n".join(
        [
            "@echo off",
            (
                r"C:\Windows\System32\rundll32.exe "
                rf"C:\Windows\System32\comsvcs.dll, MiniDump {pid} "
                rf"{windows_path} full"
            ),
            "if errorlevel 1 exit /b %errorlevel%",
            r"C:\Windows\System32\timeout.exe /t 3 /nobreak >nul",
            (
                rf"C:\Windows\System32\icacls.exe {windows_path} "
                rf'/grant "{identity}":F'
            ),
            "exit /b %errorlevel%",
            "",
        ]
    )


def create_full_dump(pid: int) -> tuple[Path, str]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    windows_path = rf"C:\Dump\weixin_voice_{stamp}_{pid}.dmp"
    wsl_path = windows_to_wsl_path(windows_path)
    batch_windows_path = rf"C:\Dump\wechat_voice_dump_{stamp}_{pid}.cmd"
    batch_wsl_path = windows_to_wsl_path(batch_windows_path)
    _powershell("New-Item -ItemType Directory -Force -Path 'C:\\Dump' | Out-Null")
    if wsl_path.exists() or batch_wsl_path.exists():
        raise ExtractError("为安全起见不覆盖已存在的 dump 或临时命令文件。")

    identity = _powershell(
        "[Security.Principal.WindowsIdentity]::GetCurrent().Name"
    ).splitlines()[-1].strip()
    batch_wsl_path.write_text(
        build_full_dump_batch(pid, windows_path, identity),
        encoding="ascii",
        newline="",
    )
    launcher = rf"""
$ErrorActionPreference = 'Stop'
$args = @('/d', '/c', '{batch_windows_path}')
$p = Start-Process -FilePath 'cmd.exe' -ArgumentList $args -Verb RunAs -WindowStyle Hidden -Wait -PassThru
if ($p.ExitCode -ne 0) {{ exit $p.ExitCode }}
"""
    print("Windows 可能弹出管理员授权窗口，用于创建 full dump。")
    try:
        _powershell(launcher, timeout=360)
    except ExtractError as exc:
        try:
            if wsl_path.is_file() and wsl_path.stat().st_size < 1024 * 1024:
                wsl_path.unlink()
        except OSError:
            pass
        raise ExtractError(
            "管理员授权未完成，无法自动创建 full dump。"
            "请在管理员任务管理器中对主 Weixin.exe 创建内存转储，"
            "然后使用 --dump 指定文件。"
        ) from exc
    finally:
        try:
            batch_wsl_path.unlink(missing_ok=True)
        except OSError:
            pass

    if not wsl_path.is_file() or wsl_path.stat().st_size < 1024 * 1024:
        try:
            wsl_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ExtractError("没有生成有效的 full dump；不完整文件已删除。")

    try:
        with wsl_path.open("rb") as handle:
            mm = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                parse_memory64_ranges(mm)
            finally:
                mm.close()
    except (OSError, ExtractError) as exc:
        try:
            wsl_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ExtractError(
            "Windows 生成的文件不是 full dump，已删除该不完整文件。"
        ) from exc
    return wsl_path, windows_path


def extract_all_head_md5s(buffer: Buffer) -> set[str]:
    values: set[str] = set()
    for needle in (b"head256md5", "head256md5".encode("utf-16le")):
        position = 0
        while True:
            position = buffer.find(needle, position)  # type: ignore[attr-defined]
            if position < 0:
                break
            start = max(0, position - 128)
            end = min(len(buffer), position + 512)
            for text in _decode_windows(bytes(buffer[start:end])):
                values.update(
                    match.lower()
                    for match in re.findall(
                        r"head256md5(?:\s*[:=]\s*|>\s*)([0-9a-fA-F]{32})",
                        text,
                        re.IGNORECASE,
                    )
                )
            position += len(needle)
    return values


def plausible_metadata(items: list[VoiceMetadata]) -> list[VoiceMetadata]:
    result = []
    for item in items:
        if item.full_size > 100 * 1024 * 1024:
            continue
        if item.data_format == "silk":
            result.append(item)
            continue
        if item.full_size % 60 != 0:
            continue
        frame_duration_ms = item.full_size // 60 * 20
        tolerance = max(5_000, int(item.duration_ms * 0.05))
        if abs(frame_duration_ms - item.duration_ms) <= tolerance:
            result.append(item)
    return result


def filter_metadata_by_duration(
    items: list[VoiceMetadata], duration_seconds: float | None
) -> list[VoiceMetadata]:
    if duration_seconds is None:
        return items
    target_ms = duration_seconds * 1000
    tolerance_ms = max(2_000, target_ms * 0.10)
    matches = [item for item in items if abs(item.duration_ms - target_ms) <= tolerance_ms]
    if matches:
        return matches
    found = ", ".join(
        sorted({_duration_label(item.duration_ms) for item in items})
    ) or "无"
    raise ExtractError(
        f"dump 中没有接近 {duration_seconds:g} 秒的语音元数据；"
        f"本次只发现：{found}。请确认是在播放收藏中的笔记语音后立即创建的 dump。"
    )


def _source_times(windows_path: str) -> tuple[float, float]:
    try:
        item = windows_to_wsl_path(windows_path)
        stat = item.stat()
        return stat.st_atime, stat.st_mtime
    except (ExtractError, OSError):
        return 0.0, 0.0


def select_voice_match(
    matches: list[VoiceMatch],
    duration_seconds: float | None,
    non_interactive: bool,
) -> VoiceMatch:
    if not matches:
        raise ExtractError(
            "没有找到同时通过 fullsize、head256md5 和 fullmd5 校验的原始 Speex/Silk。"
            "请重新完整播放收藏中的笔记语音后立即运行；当前 dump 会保留。"
        )

    groups: dict[str, list[VoiceMatch]] = {}
    for match in matches:
        groups.setdefault(match.metadata.full_md5, []).append(match)
    representatives = [values[0] for values in groups.values()]
    if duration_seconds is not None:
        representatives.sort(
            key=lambda item: abs(item.metadata.duration_ms / 1000 - duration_seconds)
        )
        if len(representatives) == 1:
            return representatives[0]
        first_delta = abs(representatives[0].metadata.duration_ms / 1000 - duration_seconds)
        second_delta = abs(representatives[1].metadata.duration_ms / 1000 - duration_seconds)
        if first_delta + 0.5 < second_delta:
            return representatives[0]

    if len(representatives) == 1:
        return representatives[0]

    ranked = []
    for item in representatives:
        atime, mtime = _source_times(item.metadata.data_path)
        ranked.append((atime, len(groups[item.metadata.full_md5]), mtime, item))
    ranked.sort(key=lambda value: value[:3], reverse=True)
    if len(ranked) == 1 or ranked[0][:2] > ranked[1][:2]:
        return ranked[0][3]

    if non_interactive or not sys.stdin.isatty():
        raise ExtractError(
            "dump 中有多条已解密语音且无法安全判断刚播放的是哪条。"
            "请用 --duration 秒数重试同一个 dump。"
        )

    print("\n检测到多条仍在内存中的语音，请选择刚才播放的一条：")
    for index, item in enumerate(representatives, 1):
        print(
            f"  {index}. {_duration_label(item.metadata.duration_ms)}  "
            f"{PureWindowsPath(item.metadata.data_path).name}"
        )
    while True:
        answer = input("输入序号：").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(representatives):
            return representatives[int(answer) - 1]
        print("请输入列表中的有效序号。")


def _duration_label(duration_ms: int) -> str:
    seconds = max(1, round(duration_ms / 1000))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}分{seconds:02d}秒" if minutes else f"{seconds}秒"


def _available_output_paths(
    output_dir: Path, duration_ms: int, data_format: str
) -> tuple[Path, Path, Path]:
    stem = f"微信语音_{_duration_label(duration_ms)}"
    wav = output_dir / f"{stem}.wav"
    mp3 = output_dir / f"{stem}.mp3"
    if wav.exists() or mp3.exists():
        stem += "_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        wav = output_dir / f"{stem}.wav"
        mp3 = output_dir / f"{stem}.mp3"
    return output_dir / f".{stem}.raw.{data_format}", wav, mp3


def _run_checked(command: list[str], label: str) -> None:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=600)
    except FileNotFoundError as exc:
        raise ExtractError(f"找不到 {label} 所需程序：{command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ExtractError(f"{label}超时。") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ExtractError(f"{label}失败：{detail[-2000:] or f'退出代码 {result.returncode}'}")


def _probe_audio(path: Path) -> dict:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration,bit_rate:stream=codec_name,sample_rate,channels,bit_rate",
                "-of", "json", str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise ExtractError("找不到 ffprobe；请先在 WSL 安装 ffmpeg。") from exc
    if result.returncode != 0:
        raise ExtractError(f"无法验证音频文件 {path.name}：{result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ExtractError(f"ffprobe 返回了无法识别的结果：{path.name}") from exc


def _verify_audio(
    wav: Path,
    mp3: Path,
    expected_duration_ms: int,
    expected_sample_rate: int,
) -> None:
    wav_info = _probe_audio(wav)
    mp3_info = _probe_audio(mp3)
    wav_stream = (wav_info.get("streams") or [{}])[0]
    mp3_stream = (mp3_info.get("streams") or [{}])[0]
    if (
        wav_stream.get("codec_name") != "pcm_s16le"
        or int(wav_stream.get("sample_rate") or 0) != expected_sample_rate
        or int(wav_stream.get("channels") or 0) != 1
    ):
        raise ExtractError(
            f"WAV 验证失败：预期为 PCM 16-bit、{expected_sample_rate} Hz、单声道。"
        )
    if (
        mp3_stream.get("codec_name") != "mp3"
        or int(mp3_stream.get("sample_rate") or 0) != expected_sample_rate
        or int(mp3_stream.get("channels") or 0) != 1
    ):
        raise ExtractError(
            f"MP3 验证失败：预期为 MP3、{expected_sample_rate} Hz、单声道。"
        )
    bit_rate = int(
        mp3_stream.get("bit_rate")
        or (mp3_info.get("format") or {}).get("bit_rate")
        or 0
    )
    if not 120_000 <= bit_rate <= 136_000:
        raise ExtractError(f"MP3 码率异常：得到 {bit_rate} bps，预期约 128000 bps。")
    duration = float((mp3_info.get("format") or {}).get("duration") or 0)
    expected = expected_duration_ms / 1000
    if duration <= 0 or abs(duration - expected) > max(2.0, expected * 0.03):
        raise ExtractError(
            f"MP3 时长异常：得到 {duration:.2f} 秒，微信元数据为 {expected:.2f} 秒。"
        )


def _desktop_path() -> Path:
    windows_path = _powershell(
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
        "[Environment]::GetFolderPath('Desktop')"
    ).splitlines()[-1].strip()
    return windows_to_wsl_path(windows_path)


def _normalise_wsl_path(value: str) -> Path:
    if re.match(r"^[A-Za-z]:[\\/]", value):
        return windows_to_wsl_path(value)
    return Path(value).expanduser().resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="播放收藏中的笔记语音后，自动恢复并保留 WAV 与 128 kbps MP3。"
    )
    parser.add_argument("--pid", type=int, help="手动指定主 Weixin.exe PID。")
    parser.add_argument("--dump", help="改成你自己的 full dump 文件路径（WSL 或 Windows 路径），跳过创建。")
    parser.add_argument("--duration", type=float, help="歧义时按目标时长（秒）筛选。")
    parser.add_argument("--output-dir", help="改成你自己的导出文件保存目录；默认是当前 Windows 用户桌面。")
    parser.add_argument(
        "--account-root",
        default=DEFAULT_ACCOUNT_ROOT,
        help="改成你自己的微信文件存储位置的盘和文件夹；不能照抄示例。",
    )
    parser.add_argument("--decoder", default=DEFAULT_DECODER, help="改成你自己的 Speex 解码器文件路径。")
    parser.add_argument(
        "--silk-decoder",
        default=DEFAULT_SILK_DECODER,
        help="改成你自己的 Silk V3 解码器文件路径。",
    )
    parser.add_argument("--keep-dump", action="store_true", help="成功后仍保留脚本创建的 dump。")
    parser.add_argument("--keep-raw", action="store_true", help="成功后仍保留 raw Speex/Silk。")
    parser.add_argument("--non-interactive", action="store_true", help="歧义时失败，不询问序号。")
    return parser


def run(args: argparse.Namespace) -> tuple[Path, Path]:
    if "microsoft" not in Path("/proc/version").read_text(errors="ignore").casefold():
        raise ExtractError("请在 WSL Ubuntu 终端里运行这个脚本。")

    if "YOUR_WECHAT_ID" in args.account_root:
        raise ExtractError(
            "请先把 --account-root 改成你自己的微信文件存储位置的盘和文件夹；"
            "不能直接使用示例路径。"
        )

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise ExtractError("找不到 ffmpeg/ffprobe；请先在 WSL 安装 ffmpeg。")

    output_dir = _normalise_wsl_path(args.output_dir) if args.output_dir else _desktop_path()
    output_dir.mkdir(parents=True, exist_ok=True)

    created_dump = args.dump is None
    dump_windows_path = ""
    if args.dump:
        dump_path = _normalise_wsl_path(args.dump)
    else:
        processes = discover_weixin_processes()
        selected_process = (
            next((item for item in processes if item.pid == args.pid), None)
            if args.pid is not None
            else choose_main_weixin(processes)
        )
        if selected_process is None:
            raise ExtractError(f"指定的 PID {args.pid} 不是当前 Weixin.exe 进程。")
        print(
            f"已选择主 Weixin.exe：PID {selected_process.pid}，"
            f"内存约 {selected_process.working_set / 1024 / 1024:.0f} MB。"
        )
        print("安全提示：full dump 含敏感微信内存；仅成功后自动删除，失败时会保留供重试。")
        dump_path, dump_windows_path = create_full_dump(selected_process.pid)
        print(f"full dump 已创建：{dump_windows_path}")

    if not dump_path.is_file():
        raise ExtractError(f"找不到 dump：{dump_path}")

    raw_path: Path | None = None
    pcm_path: Path | None = None
    succeeded = False
    try:
        print(f"正在分析 dump（{dump_path.stat().st_size / 1024 / 1024:.0f} MB）……")
        with dump_path.open("rb") as handle:
            mm = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                ranges = parse_memory64_ranges(mm)
                metadata = plausible_metadata(extract_voice_metadata(mm, args.account_root))
                account_metadata = [item for item in metadata if item.account_path_match]
                if not account_metadata:
                    raise ExtractError(
                        "没有在你微信文件存储位置的盘和文件夹下找到可信的 Speex/Silk 元数据。"
                        "请确认 --account-root，或重新播放收藏中的笔记语音后重试。"
                    )
                account_metadata = filter_metadata_by_duration(
                    account_metadata, args.duration
                )
                formats = "/".join(sorted({item.data_format for item in account_metadata}))
                print(
                    f"找到 {len(account_metadata)} 条目标附近的可信语音元数据，"
                    f"开始定位已解密 {formats}……"
                )

                cache_path = Path(HEAD_CACHE).expanduser()
                known_head = (
                    cache_path.read_bytes()
                    if cache_path.is_file()
                    and any(item.data_format == "speex" for item in account_metadata)
                    else None
                )
                global_heads = extract_all_head_md5s(mm)
                matches = find_voice_matches(
                    mm,
                    ranges,
                    account_metadata,
                    known_head=known_head,
                    extra_head_md5s=global_heads,
                )
                selected = select_voice_match(matches, args.duration, args.non_interactive)
                metadata_item = selected.metadata
                actual_head_md5 = hashlib.md5(selected.head_bytes).hexdigest()

                print("\n收藏中的笔记语音元数据：")
                print(f"  format     : {metadata_item.data_format}")
                print(f"  duration   : {metadata_item.duration_ms} ms")
                print(f"  fullsize   : {metadata_item.full_size}")
                print(f"  fullmd5    : {metadata_item.full_md5}")
                print(f"  head256md5 : {actual_head_md5}")
                print(f"  datapath   : {metadata_item.data_path}")
                print(f"  virtual VA : 0x{selected.virtual_address:x}")

                if metadata_item.data_format == "speex":
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_bytes(selected.head_bytes)
                    try:
                        cache_path.chmod(0o600)
                    except OSError:
                        pass

                raw_path, wav_path, mp3_path = _available_output_paths(
                    output_dir, metadata_item.duration_ms, metadata_item.data_format
                )
                payload = bytes(
                    mm[selected.file_offset: selected.file_offset + metadata_item.full_size]
                )
                raw_path.write_bytes(payload)
            finally:
                mm.close()

        if metadata_item.data_format == "speex":
            decoder = Path(args.decoder).expanduser()
            if not decoder.is_file() or not os.access(decoder, os.X_OK):
                raise ExtractError(
                    f"找不到可执行 Speex 解码器：{decoder}\n"
                    "请确认 ~/wechat-speex-declib/bin/speex_decode 已经编译成功。"
                )
            _run_checked([str(decoder), str(raw_path), str(wav_path)], "Speex 转 WAV")
            expected_sample_rate = 16_000
        elif metadata_item.data_format == "silk":
            silk_decoder = Path(args.silk_decoder).expanduser()
            if not silk_decoder.is_file() or not os.access(silk_decoder, os.X_OK):
                raise ExtractError(
                    f"找不到可执行 Silk 解码器：{silk_decoder}\n"
                    "请确认 ~/.local/bin/silk_v3_decoder 已安装。"
                )
            pcm_path = raw_path.with_suffix(".pcm")
            _run_checked(
                [str(silk_decoder), str(raw_path), str(pcm_path), "-quiet"],
                "Silk 转 PCM",
            )
            _run_checked(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "s16le", "-ar", "24000", "-ac", "1",
                    "-i", str(pcm_path), str(wav_path),
                ],
                "PCM 封装 WAV",
            )
            expected_sample_rate = 24_000
        else:
            raise ExtractError(f"暂不支持语音格式：{metadata_item.data_format}")

        _run_checked(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(wav_path), "-c:a", "libmp3lame", "-b:a", "128k", str(mp3_path),
            ],
            "WAV 转 128 kbps MP3",
        )
        _verify_audio(
            wav_path,
            mp3_path,
            metadata_item.duration_ms,
            expected_sample_rate,
        )
        succeeded = True

        if raw_path and not args.keep_raw:
            try:
                raw_path.unlink(missing_ok=True)
            except OSError as exc:
                print(f"警告：无法删除临时 raw 语音，请稍后手动删除：{raw_path}（{exc}）")
        if pcm_path:
            try:
                pcm_path.unlink(missing_ok=True)
            except OSError as exc:
                print(f"警告：无法删除临时 PCM，请稍后手动删除：{pcm_path}（{exc}）")
        if created_dump and not args.keep_dump:
            try:
                dump_path.unlink(missing_ok=True)
            except OSError as exc:
                print(f"警告：无法删除敏感 dump，请尽快手动删除：{dump_path}（{exc}）")
        return wav_path, mp3_path
    finally:
        if not succeeded:
            print("\n提取未完成：为便于安全重试，dump 和已生成的中间文件都没有删除。")
            print(f"保留的 dump：{dump_path}")
            if created_dump:
                print(f"可用同一文件重试：python3 {Path(__file__).resolve()} --dump '{dump_path}'")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        wav_path, mp3_path = run(args)
    except ExtractError as exc:
        print(f"\n失败：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消；敏感 dump 不会在未成功时自动删除。", file=sys.stderr)
        return 130

    print("\n提取成功，已保留：")
    print(f"  WAV：{wav_path}")
    print(f"  MP3：{mp3_path}")
    if args.keep_raw:
        print("已按 --keep-raw 要求保留 raw Speex/Silk。")
    else:
        print("临时 raw Speex/Silk 已按默认规则清理。")
    if args.dump:
        print("你提供的原有 dump 未被删除。")
    elif args.keep_dump:
        print("已按 --keep-dump 要求保留脚本创建的敏感 dump。")
    else:
        print("脚本创建的敏感 dump 已按默认规则清理。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

