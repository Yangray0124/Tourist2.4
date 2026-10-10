"""PA1 v2 Cloud Run attachment grader (discord.py 2.x).
Only this Bot file is changed. The existing loader discovers the Cog.
AWS NEVER compiles/executes uploaded assembly or starts Docker.
PA1_CLOUD_RUN_URL: HTTPS *.run.app service URL, without /grade.
PA1_GOOGLE_CREDENTIALS_PATH: environment, then ignored keys.py.
PA1_CLOUD_RUN_URL also accepts ignored keys.py. Private IAM only.
PA1_QUEUE_SIZE: 1..32 (default 8); PA1_WORKERS: 1..4 (default 1).
HTTPS errors/missing credentials return UNAVAILABLE, never host fallback.
Cloud Run deployment files live in the independent sibling pa1-cloud-run/.
All original tests/oracles/machine checks have moved to its grader_core.py.
ZIPs are read in bounded memory without extracting archive paths.
Only four exact .s names and <studentID>_PA1.zip trigger grading.
PDFs and unrelated attachments are silently left to existing Cogs.
Permissions: view/send/thread send/embed/attach/history and Message Content.
No global event handler or command is replaced.
CLI: --environment-check, --engine-self-test, --grade FILE (remote only).
Official hidden cases are unavailable; reported scores are eligibility only.
"""

from __future__ import annotations
import asyncio
import base64
import hashlib
import importlib.util
import io
import json
import logging
import os
import platform
import re
import stat
import sys
import time
import uuid
import urllib.parse
import zipfile
import zlib
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import timezone
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Literal
LOG = logging.getLogger(__name__)
SEED = 20261005
MAX_SOURCE = 128 * 1024
MAX_LINES = 6000
MAX_REPORT = 7 * 1024 * 1024
MAX_ARCHIVE = 2 * 1024 * 1024
MAX_EXPANDED = 8 * 1024 * 1024
MAX_MEMBERS = 16
MAX_RESPONSE = 32 * 1024 * 1024
ENTRY = {"p2.s": "range_sum_bst", "p3_1.s": "gf28_mul",
         "p3_2.s": "aes_sbox_calc", "p4.s": "freq_count"}
PA1_ZIP_NAME = re.compile(r"(?P<folder>[A-Z][0-9]+_PA1)\.zip", re.ASCII)
POINTS = {"p2.s": (20, 0, 8), "p3_1.s": (10, 5, 10),
          "p3_2.s": (15, 0, 10), "p4.s": (20, 10, 10)}
M_OPS = frozenset(("mul", "mulh", "mulhsu", "mulhu", "div", "divu", "rem", "remu"))
LOADS = frozenset(("lb", "lbu", "lh", "lhu", "lw"))


class GraderError(Exception):
    """A reported compatibility, submission or resource error."""

class CompatibilityError(GraderError):
    """The local engine cannot establish Jupiter-equivalent execution."""

class EngineUnavailable(GraderError):
    """Tools or isolation are unavailable; this is not a student failure."""

class SubmissionFormatError(GraderError):
    """An attachment/submission format error, not a computation failure."""


def attachment_kind(filename: str) -> Literal["assembly", "archive"] | None:
    """Classify names only, before touching size/content or the queue."""
    if filename in ENTRY:
        return "assembly"
    return "archive" if PA1_ZIP_NAME.fullmatch(filename) else None


@dataclass
class ArchiveBundle:
    sources: dict[str, str]
    folder: str


def unpack_archive(raw: bytes, filename: str) -> ArchiveBundle:
    """Validate the official four-file layout, then read bounded .s bytes only."""
    match = PA1_ZIP_NAME.fullmatch(filename)
    if match is None:
        raise SubmissionFormatError("ZIP 檔名須為 <studentID>_PA1.zip，學號開頭字母大寫。")
    folder = match.group("folder")
    if len(filename) > 120:
        raise SubmissionFormatError("ZIP 檔名超過測試服務的 120 字元安全上限。")
    if not raw or len(raw) > MAX_ARCHIVE:
        raise SubmissionFormatError(f"ZIP 大小需為 1..{MAX_ARCHIVE} bytes。")
    required = {f"{folder}/{name}": name for name in ENTRY}
    selected: dict[str, zipfile.ZipInfo] = {}
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if not members or len(members) > MAX_MEMBERS:
                raise SubmissionFormatError(f"ZIP 項目數須為 1..{MAX_MEMBERS}（包含資料夾）。")
            expanded = 0
            for member in members:
                name = member.filename
                path = PurePosixPath(name)
                parts = name.rstrip('/').split('/')
                if (not name or member.orig_filename != name or len(name) > 240 or '\\' in name or ':' in name or '\0' in name or
                    path.is_absolute() or any(part in ('', '.', '..') for part in parts) or len(parts) > 4):
                    raise SubmissionFormatError(f"ZIP 含不安全或有歧義的路徑：{name!r}。")
                key = name.rstrip('/').casefold()
                if key in seen:
                    raise SubmissionFormatError(f"ZIP 路徑重複（含大小寫衝突）：{name}。")
                seen.add(key)
                file_type = stat.S_IFMT(member.external_attr >> 16)
                if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise SubmissionFormatError("ZIP 不允許符號連結、特殊裝置或其他特殊檔案。")
                is_dir = member.is_dir()
                if ((is_dir and file_type == stat.S_IFREG) or
                    (not is_dir and (file_type == stat.S_IFDIR or member.external_attr & 0x10))):
                    raise SubmissionFormatError("ZIP 的檔案／資料夾屬性不一致。")
                if parts[0] != folder:
                    raise SubmissionFormatError(f"頂層資料夾須為 {folder}/，且學號須與 ZIP 檔名一致。")
                if is_dir:
                    if name != folder + '/' or member.file_size != 0:
                        raise SubmissionFormatError("ZIP 只允許正式頂層資料夾，不允許額外／巢狀資料夾。")
                elif name not in required:
                    # Reject extra PDFs by path only, without opening/reading them.
                    raise SubmissionFormatError(f"ZIP 含錯誤名稱、錯誤目錄或額外檔案：{name}；只允許指定四份 .s。")
                if member.flag_bits & 0x41 or member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise SubmissionFormatError("ZIP 不支援加密或此種壓縮方式。")
                expanded += member.file_size
                if expanded > MAX_EXPANDED:
                    raise SubmissionFormatError("ZIP 解壓後大小超過 8 MiB 安全上限。")
                if is_dir:
                    continue
                if not 0 < member.file_size <= MAX_SOURCE:
                    raise SubmissionFormatError(f"ZIP 的 {required[name]} 大小需為 1..{MAX_SOURCE} bytes。")
                selected[required[name]] = member
            missing = [name for name in ENTRY if name not in selected]
            if missing:
                raise SubmissionFormatError("ZIP 缺少必要檔案：" + "、".join(missing) + "。")
            # Explicit folder entry is optional: four nested paths represent it.
            # Metadata is checked first, so an extra PDF is never decompressed.
            sources: dict[str, str] = {}
            for leaf in ENTRY:
                member = selected[leaf]
                with archive.open(member) as stream:
                    content = stream.read(MAX_SOURCE + 1)
                if len(content) > MAX_SOURCE or len(content) != member.file_size:
                    raise SubmissionFormatError("ZIP 檔案大小／CRC 不符。")
                try:
                    sources[leaf] = validate_source(content)
                except GraderError as exc:
                    raise SubmissionFormatError(f"ZIP 的 {leaf} 不是有效 Assembly 文字：{exc}") from exc
    except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError, OSError, ValueError, UnicodeError) as exc:
        raise SubmissionFormatError(f"ZIP 無法完整讀取或驗證（{type(exc).__name__}）。") from exc
    return ArchiveBundle(sources, folder)


def bounded_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
        if low <= value <= high:
            return value
    except ValueError:
        LOG.warning("Invalid integer configuration: %s", name)
    LOG.warning("Using default for %s (allowed %d..%d)", name, low, high)
    return default

def validate_source(raw: bytes) -> str:
    if not raw or len(raw) > MAX_SOURCE:
        raise GraderError(f"File must contain 1..{MAX_SOURCE} bytes")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise GraderError("Assembly must be UTF-8 text") from exc
    if "\0" in text or any(ord(c) < 32 and c not in "\n\r\t" for c in text):
        raise GraderError("Binary/control characters are not accepted")
    if len(text.splitlines()) > MAX_LINES:
        raise GraderError(f"Source exceeds {MAX_LINES} lines")
    return text

@dataclass(frozen=True)
class Statement:
    line: int
    labels: tuple[str, ...]
    op: str
    args: str
    section: str

    def assembly(self) -> str:
        prefix = " ".join(f"{name}:" for name in self.labels)
        return f"{prefix} {self.op} {self.args}".strip()

DIRECTIVES = frozenset((".text", ".data", ".bss", ".rodata", ".section",
    ".align", ".palign", ".balign", ".p2align", ".word", ".long", ".4byte", ".half",
    ".short", ".2byte", ".byte", ".ascii", ".asciz", ".asciiz", ".string", ".space", ".float", ".file",
    ".zero", ".skip", ".globl", ".global", ".type", ".size", ".equ", ".set",
    ".eqv", ".option", ".insn"))

def tokenize(source: str) -> list[Statement]:
    """Tokenize quoted strings, comments, multiple statements and exact labels."""
    result: list[Statement] = []
    section = ".text"
    block_comment = False
    seen: set[str] = set()
    for number, raw in enumerate(source.splitlines(), 1):
        chunks: list[str] = []
        current: list[str] = []
        quote, escaped, index = "", False, 0
        while index < len(raw):
            char, pair = raw[index], raw[index:index + 2]
            index += 1
            if block_comment:
                if pair == "*/":
                    block_comment = False
                    index += 1
                continue
            if quote:
                current.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = ""
                continue
            if pair == "/*":
                block_comment = True
                current.append(" ")
                index += 1
            elif char == "#" or pair == "//":
                break
            elif char in "\"'":
                quote = char
                current.append(char)
            elif char == ";":
                chunks.append("".join(current))
                current = []
            else:
                current.append(char)
        if quote:
            raise GraderError(f"L{number}: unterminated string")
        chunks.append("".join(current))
        for chunk in chunks:
            chunk = chunk.strip()
            labels: list[str] = []
            while match := re.match(r"^([A-Za-z_.$][\w.$]*|[0-9]+)\s*:\s*", chunk):
                name = match.group(1)
                if name in seen and not name.isdecimal():
                    raise GraderError(f"L{number}: duplicate label {name}")
                seen.add(name)
                labels.append(name)
                chunk = chunk[match.end():]
            parts = chunk.split(None, 1)
            op = parts[0].lower() if parts else ""
            args = parts[1] if len(parts) > 1 else ""
            if op.startswith("."):
                if op not in DIRECTIVES:
                    raise GraderError(f"L{number}: unsupported/unsafe directive {op}; "
                                      "includes, binary imports and macros are disabled")
                if op == ".section":
                    target = args.split(",")[0].strip()
                    if target not in (".text", ".data", ".bss", ".rodata"):
                        raise GraderError(f"L{number}: unsupported section {target}")
                    section = target
                elif op in (".text", ".data", ".bss", ".rodata"):
                    section = op
                elif op == ".option" and args.strip() not in ("norvc", "norelax", "push", "pop"):
                    raise GraderError(f"L{number}: ISA-changing .option is not accepted")
            result.append(Statement(number, tuple(labels), op, args, section))
    if block_comment:
        raise GraderError("Unterminated block comment")
    return result

@dataclass
class Scope:
    statements: list[Statement]
    selected: list[Statement]
    risks: list[str]
    text: str

def scope_source(source: str, filename: str) -> Scope:
    """Symbol/basic-block dependency closure, including calls and fallthrough.

    Keeps helpers on either side of entry, but warns about pre-entry custom code.
    Pre-entry custom DATA is deliberately not supplied to the local harness.
    freq_array is the documented exception, synthesized by the grader.
    """
    statements = tokenize(source)
    blocks: list[list[Statement]] = []
    for stmt in statements:
        if not blocks or stmt.labels or stmt.section != blocks[-1][-1].section:
            blocks.append([])
        blocks[-1].append(stmt)
    symbols: dict[str, int] = {}
    numeric: dict[str, list[int]] = {}
    for index, block in enumerate(blocks):
        for stmt in block:
            for label in stmt.labels:
                if label.isdecimal():
                    numeric.setdefault(label, []).append(index)
                else:
                    symbols[label] = index
    root = ENTRY[filename]
    roots = [root] + (["gf28_mul"] if filename == "p3_2.s" else [])
    for name in roots:
        if name not in symbols or blocks[symbols[name]][0].section != ".text":
            raise GraderError(f"Required text label missing: {name}")
    first_line = blocks[symbols[root]][0].line
    selected: set[int] = set()
    pending = [symbols[name] for name in roots]
    risks: list[str] = []
    while pending:
        index = pending.pop()
        if index in selected:
            continue
        block = blocks[index]
        if block[0].section != ".text" and block[0].line < first_line:
            names = [label for stmt in block for label in stmt.labels]
            if filename == "p4.s" and "freq_array" in names:
                continue
            risks.append(f"L{block[0].line}: pre-entry data {names} omitted by Scoping Note")
            continue
        selected.add(index)
        if block[0].section == ".text" and block[0].line < first_line:
            risks.append(f"L{block[0].line}: pre-entry helper retained locally; official extraction uncertain")
        for stmt in block:
            if stmt.op in (".ascii", ".asciz", ".asciiz", ".string"):
                continue
            for token in re.findall(r"[A-Za-z_.$][\w.$]*|[0-9]+[bf]\b", stmt.args):
                if token in symbols:
                    pending.append(symbols[token])
                elif re.fullmatch(r"[0-9]+[bf]", token):
                    choices = numeric.get(token[:-1], [])
                    candidates = [i for i in choices if (i <= index if token[-1] == "b" else i > index)]
                    if candidates:
                        pending.append(max(candidates) if token[-1] == "b" else min(candidates))
        executable = [s for s in block if s.op and not s.op.startswith(".")]
        last = executable[-1] if executable else None
        terminal = last is not None and (last.op in ("ret", "jr", "j", "tail") or
                    (last.op in ("jal", "jalr") and last.args.replace(" ", "").startswith(("zero,", "x0,"))))
        if block[0].section == ".text" and not terminal and index + 1 < len(blocks):
            if blocks[index + 1][0].section == ".text":
                pending.append(index + 1)
    kept = [s for i, block in enumerate(blocks) if i in selected for s in block]
    # Constants/directives are safe, but pre-entry constants may not survive extraction.
    for stmt in statements:
        if stmt.op in (".equ", ".eqv", ".set") and stmt not in kept:
            kept.append(stmt)
            if stmt.line < first_line:
                risks.append(f"L{stmt.line}: pre-entry symbolic constant retained locally; verify official scope")
    kept.sort(key=lambda s: s.line)
    output = ['.option norvc', '.option norelax', '.file 1 "student.s"']
    section = ""
    for stmt in kept:
        if stmt.op in (".globl", ".global", ".type", ".size", ".file", ".text", ".data", ".bss", ".rodata", ".section"):
            continue
        if stmt.section != section:
            section = stmt.section
            output.append(f".section {section}")
        output.append(f".loc 1 {stmt.line} 0")
        aliases = {".eqv": ".equ", ".asciiz": ".asciz", ".palign": ".align"}
        output.append(Statement(stmt.line, stmt.labels, aliases.get(stmt.op, stmt.op), stmt.args, stmt.section).assembly())
    if filename == "p4.s" and not any("freq_array" in s.labels for s in kept):
        output.extend((".data", ".balign 4", "freq_array: .space 1024"))
    output.extend((".text", f".globl {root}"))
    return Scope(statements, kept, list(dict.fromkeys(risks)), "\n".join(output) + "\n")

def source_checks(scope: Scope, filename: str) -> dict[str, Any]:
    data_labels = {label for stmt in scope.statements if stmt.section != ".text" for label in stmt.labels}
    findings: list[dict[str, Any]] = []
    for stmt in scope.selected:
        if stmt.section != ".text":
            continue
        reason = ""
        kind = ""
        if stmt.op in M_OPS:
            kind, reason = "RV32M", "RV32M; only affects P3-1/P4 Bonus, not basic functionality"
        elif filename == "p3_2.s" and stmt.op in LOADS:
            kind = "Zero-LUT"
            stack = bool(re.search(r"\(\s*(sp|x2)\s*\)", stmt.args))
            reason = ("Literal PDF rule prohibits loads; stack restore/ABI ambiguity" if stack
                      else "Literal PDF rule prohibits memory loads; LUT intent requires access analysis")
        elif filename == "p3_2.s" and stmt.op in ("la", "lla"):
            targets = set(re.findall(r"[A-Za-z_.$][\w.$]*", stmt.args)) & data_labels
            if targets:
                kind, reason = "Zero-LUT", f"Address of data label: {', '.join(sorted(targets))}"
        elif stmt.op in (".word", ".long", ".4byte", ".insn"):
            kind, reason = "Encoded instruction", "Machine-code inspection required; Jupiter 3.1 rejects .word in .text (compatibility risk)"
            risk = f"L{stmt.line}: {stmt.op} in text is accepted by GNU but may be rejected by Jupiter; verify official compatibility"
            if risk not in scope.risks:
                scope.risks.append(risk)
        if reason:
            findings.append({"line": stmt.line, "instruction": stmt.assembly(), "kind": kind, "reason": reason})
    return {"source_findings": findings, "scope_risks": scope.risks,
            "machine_checked": False, "algorithm": "Human review: 8-iteration Shift-and-XOR" if filename == "p3_1.s" else None}

def stack_load_reference_sources() -> dict[str, str]:
    """Known fixtures for the engine's own tests, never substitutes for submissions.

    P2 intentionally uses a 36-byte frame. AES intentionally restores the stack
    to demonstrate the literal Zero-LUT/ABI conflict. These are independent of
    the user's supplied files; the Python oracles remain separate algorithms.
    """
    gf = """
.text
gf28_mul:
    li t0, 0
    li t1, 8
gf_test_loop:
    andi t2, a1, 1
    beqz t2, gf_test_skip
    xor t0, t0, a0
gf_test_skip:
    andi t2, a0, 128
    slli a0, a0, 1
    andi a0, a0, 255
    beqz t2, gf_test_reduce
    xori a0, a0, 27
gf_test_reduce:
    srli a1, a1, 1
    addi t1, t1, -1
    bnez t1, gf_test_loop
    mv a0, t0
    ret
"""
    bst = """
.text
range_sum_bst:
    li t0, -1
    beq a1, t0, bst_test_empty
    addi sp, sp, -36
    sw ra, 0(sp)
    sw s0, 4(sp)
    sw s1, 8(sp)
    sw s2, 12(sp)
    sw s3, 16(sp)
    sw s4, 20(sp)
    mv s0, a0
    mv s2, a2
    mv s3, a3
    slli t0, a1, 2
    slli t1, a1, 3
    add t0, t0, t1
    add s1, a0, t0
    li s4, 0
    lw t0, 0(s1)
    blt t0, s2, bst_test_left
    bgt t0, s3, bst_test_left
    mv s4, t0
bst_test_left:
    mv a0, s0
    lw a1, 4(s1)
    mv a2, s2
    mv a3, s3
    jal ra, range_sum_bst
    add s4, s4, a0
    mv a0, s0
    lw a1, 8(s1)
    mv a2, s2
    mv a3, s3
    jal ra, range_sum_bst
    add a0, s4, a0
    lw ra, 0(sp)
    lw s0, 4(sp)
    lw s1, 8(sp)
    lw s2, 12(sp)
    lw s3, 16(sp)
    lw s4, 20(sp)
    addi sp, sp, 36
    ret
bst_test_empty:
    li a0, 0
    ret
"""
    aes = """
.text
aes_sbox_calc:
    addi sp, sp, -16
    sw ra, 0(sp)
    sw s0, 4(sp)
    sw s1, 8(sp)
    sw s2, 12(sp)
    mv s0, a0
    li s1, 1
    li s2, 254
aes_test_power:
    andi t0, s2, 1
    beqz t0, aes_test_square
    mv a0, s1
    mv a1, s0
    jal ra, gf28_mul
    mv s1, a0
aes_test_square:
    mv a0, s0
    mv a1, s0
    jal ra, gf28_mul
    mv s0, a0
    srli s2, s2, 1
    bnez s2, aes_test_power
    xori a0, s1, 99
    li t2, 4
aes_test_rotate:
    slli t0, s1, 1
    srli t1, s1, 7
    or s1, t0, t1
    andi s1, s1, 255
    xor a0, a0, s1
    addi t2, t2, -1
    bnez t2, aes_test_rotate
    lw ra, 0(sp)
    lw s0, 4(sp)
    lw s1, 8(sp)
    lw s2, 12(sp)
    addi sp, sp, 16
    ret
""" + gf
    freq = """
.data
.balign 4
freq_array: .space 1024
.text
freq_count:
    la a7, freq_array
    mv t0, a7
    addi t1, a7, 1024
freq_test_clear:
    sw zero, 0(t0)
    addi t0, t0, 4
    bne t0, t1, freq_test_clear
freq_test_input:
    lbu t0, 0(a0)
    beqz t0, freq_test_output
    slli t0, t0, 2
    add t0, a7, t0
    lw t1, 0(t0)
    addi t1, t1, 1
    sw t1, 0(t0)
    addi a0, a0, 1
    j freq_test_input
freq_test_output:
    li a2, 32
freq_test_char:
    slli t0, a2, 2
    add t0, a7, t0
    lw t2, 0(t0)
    beqz t2, freq_test_next
    li t3, 0
    li t4, 0
    li t0, 100
freq_test_hundreds:
    blt t2, t0, freq_test_tens_init
    addi t2, t2, -100
    addi t3, t3, 1
    j freq_test_hundreds
freq_test_tens_init:
    li t0, 10
freq_test_tens:
    blt t2, t0, freq_test_emit
    addi t2, t2, -10
    addi t4, t4, 1
    j freq_test_tens
freq_test_emit:
    beqz t3, freq_test_no_hundreds
    addi t0, t3, 48
    sb t0, 0(a1)
    addi a1, a1, 1
    j freq_test_write_tens
freq_test_no_hundreds:
    beqz t4, freq_test_ones
freq_test_write_tens:
    addi t0, t4, 48
    sb t0, 0(a1)
    addi a1, a1, 1
freq_test_ones:
    addi t0, t2, 48
    sb t0, 0(a1)
    sb a2, 1(a1)
    addi a1, a1, 2
freq_test_next:
    addi a2, a2, 1
    li t0, 127
    bne a2, t0, freq_test_char
    sb zero, 0(a1)
    ret
"""
    return {"p2.s": bst, "p3_1.s": gf, "p3_2.s": aes, "p4.s": freq}




def trusted_reference_sources() -> dict[str, str]:
    """Load-free AES plus the preserved BST/GF/frequency reference fixtures."""
    sources = stack_load_reference_sources()
    # The local gf28_mul implementation uses only a0/a1/t0/t1/t2. It explicitly
    # preserves a2-a7, so this closed pair needs no memory to retain caller state.
    # It still preserves all standard callee-saved registers, sp and return ra.
    sources["p3_2.s"] = """
.text
aes_sbox_calc:
    mv a2, ra
    mv a3, a0
    li a4, 1
    li a5, 254
aes_no_load_power:
    andi t5, a5, 1
    beqz t5, aes_no_load_square
    mv a0, a4
    mv a1, a3
    jal ra, gf28_mul
    mv a4, a0
aes_no_load_square:
    mv a0, a3
    mv a1, a3
    jal ra, gf28_mul
    mv a3, a0
    srli a5, a5, 1
    bnez a5, aes_no_load_power
    xori a0, a4, 99
    li t2, 4
aes_no_load_rotate:
    slli t0, a4, 1
    srli t1, a4, 7
    or a4, t0, t1
    andi a4, a4, 255
    xor a0, a0, a4
    addi t2, t2, -1
    bnez t2, aes_no_load_rotate
    mv ra, a2
    ret
""" + sources["p3_1.s"]
    return sources


def local_setting(name: str) -> tuple[str, str]:
    """Environment overrides the trusted, ignored local keys.py."""
    if name in os.environ:
        return os.environ[name].strip(), "environment"
    module = sys.modules.get("keys")
    if module is None:
        path = Path(__file__).resolve().parent.parent / "keys.py"
        if not path.is_file():
            return "", "unset"
        try:
            spec = importlib.util.spec_from_file_location("_pa1_local_settings", path)
            if spec is None or spec.loader is None:
                raise ValueError("settings loader unavailable")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:
            raise EngineUnavailable("無法讀取本機 keys.py 設定；請檢查語法與讀取權限。") from None
    value = getattr(module, name, "")
    if not isinstance(value, str):
        raise EngineUnavailable(f"{name} 必須是字串設定。")
    return value.strip(), "keys.py" if value else "unset"


class RemoteEngine:
    """Private Cloud Run, with short-lived service-account ID tokens only."""
    def __init__(self) -> None:
        self.config_error = ""
        try:
            self.url, self.url_source = local_setting("PA1_CLOUD_RUN_URL")
            raw_path, self.path_source = local_setting("PA1_GOOGLE_CREDENTIALS_PATH")
        except EngineUnavailable as exc:
            self.config_error = str(exc)
            self.url, raw_path = "", ""
            self.url_source = self.path_source = "invalid"
        self.url = self.url.rstrip("/")
        self.credentials_path: Path | None = None
        try:
            self.credentials_path = Path(raw_path).expanduser() if raw_path else None
            if self.credentials_path is not None and not self.credentials_path.is_absolute():
                self.credentials_path = Path(__file__).resolve().parent.parent / self.credentials_path
        except (OSError, ValueError, RuntimeError):
            self.config_error = "憑證路徑設定無法解析；請檢查本機設定。"
        self.mode = "iam"
        self.credentials: Any = None
        self.session: Any = None
        self.lock = asyncio.Lock()
        self.token_lock = asyncio.Lock()
        self.token = ""
        self.token_until = 0.0
        self.closed = False
        self.request_times: deque[float] = deque()
        self.rate = bounded_env("PA1_CLIENT_RATE_PER_MINUTE", 6, 1, 30)

    def configured(self) -> None:
        if self.config_error:
            raise EngineUnavailable(self.config_error)
        try:
            parsed = urllib.parse.urlsplit(self.url)
            port = parsed.port
        except ValueError:
            raise EngineUnavailable("PA1_CLOUD_RUN_URL 格式無法解析。") from None
        if (parsed.scheme != "https" or not parsed.hostname or
            not parsed.hostname.endswith(".run.app") or parsed.username or parsed.password or
            parsed.query or parsed.fragment or parsed.path not in ("", "/") or port not in (None, 443)):
            raise EngineUnavailable("PA1_CLOUD_RUN_URL 未正確設定：需為 HTTPS Cloud Run 服務網址，不包含 /grade。")
        if self.credentials_path is None:
            raise EngineUnavailable("PA1_GOOGLE_CREDENTIALS_PATH 未設定：請使用環境變數或本機 keys.py。")

    def credential_info(self) -> dict[str, Any]:
        self.configured()
        assert self.credentials_path is not None
        try:
            metadata = self.credentials_path.stat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 65536:
                raise EngineUnavailable("憑證需為不超過 64 KiB 的 JSON 一般檔案。")
            if os.name == "posix" and stat.S_IMODE(metadata.st_mode) & 0o077:
                raise EngineUnavailable("JSON 憑證權限過寬；Linux 請將該檔設為 chmod 600（或 400）。")
            with self.credentials_path.open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise EngineUnavailable("JSON 憑證超過 64 KiB。")
            info = json.loads(raw)
        except EngineUnavailable:
            raise
        except FileNotFoundError:
            raise EngineUnavailable("找不到 JSON 憑證；請檢查 PA1_GOOGLE_CREDENTIALS_PATH。") from None
        except PermissionError:
            raise EngineUnavailable("JSON 憑證無法讀取；請檢查檔案權限。") from None
        except (OSError, ValueError, UnicodeError):
            raise EngineUnavailable("JSON 憑證無法讀取或格式錯誤。") from None
        if (not isinstance(info, dict) or info.get("type") != "service_account" or
            info.get("token_uri") != "https://oauth2.googleapis.com/token" or
            info.get("universe_domain", "googleapis.com") != "googleapis.com" or
            not isinstance(info.get("client_email"), str) or
            not re.fullmatch(r"[^@\s]+@[^@\s]+\.iam\.gserviceaccount\.com", info["client_email"])):
            raise EngineUnavailable("需使用 Google Service Account JSON 憑證與官方 token endpoint。")
        return info

    def mint_id_token(self) -> tuple[str, float]:
        """Synchronous Google refresh; called only through asyncio.to_thread."""
        try:
            from google.oauth2.service_account import IDTokenCredentials
            from google.auth.transport.requests import Request
        except ImportError:
            raise EngineUnavailable("認證需要 google-auth 與 requests；請另行安裝至 Bot 的 Python 環境。") from None
        try:
            if self.credentials is None:
                info = self.credential_info()
                self.credentials = IDTokenCredentials.from_service_account_info(info, target_audience=self.url)
            transport = Request()
            def timed_transport(*args: Any, **kwargs: Any) -> Any:
                kwargs["timeout"] = 20
                return transport(*args, **kwargs)
            self.credentials.refresh(timed_transport)
            token, expiry = self.credentials.token, self.credentials.expiry
            if not isinstance(token, str) or token.count(".") != 2 or len(token) > 12000 or expiry is None:
                raise ValueError("invalid token metadata")
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            until = min(time.time() + 3600, expiry.timestamp()) - 120
            if until <= time.time():
                raise ValueError("token already near expiry")
            return token, until
        except EngineUnavailable:
            raise
        except Exception:
            # No credential paths, Google response bodies or token values leave here.
            raise EngineUnavailable("Google ID Token 取得失敗；請檢查金鑰是否有效、系統時間與 HTTPS 網路。") from None

    async def auth_headers(self, body: bytes = b"", force_refresh: bool = False) -> dict[str, str]:
        async with self.token_lock:
            if force_refresh or time.time() >= self.token_until:
                self.token, self.token_until = await asyncio.to_thread(self.mint_id_token)
            return {"Authorization": "Bearer " + self.token}

    async def heartbeat(self, progress: Callable[[dict[str, Any]], Awaitable[None]] | None,
                        started: float) -> None:
        if progress is None:
            return
        while True:
            await asyncio.sleep(15)
            try:
                await progress({"event": "remote_wait", "elapsed": int(time.monotonic() - started)})
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.warning("PA1 remote progress update failed; grading continues")

    def check_response(self, payload: Any, sources: dict[str, str]) -> list[dict[str, Any]]:
        plan = {"p2.s": 1078, "p3_1.s": 65542, "p3_2.s": 262, "p4.s": 1049}
        if (not isinstance(payload, dict) or payload.get("schema_version") != 1 or
            payload.get("seed") != SEED or payload.get("plan") != {name: plan[name] for name in sources}):
            raise EngineUnavailable("Cloud Run 回報版本／測試覆蓋率不符；不會標示測試通過。")
        expected_core = os.environ.get("PA1_EXPECTED_CORE_SHA256", "")
        if expected_core and payload.get("core_sha256") != expected_core:
            raise EngineUnavailable("Cloud Run 測試核心 SHA256 與部署核對值不同；請核對版本。")
        reports = payload.get("reports")
        if (not isinstance(reports, list) or len(reports) != len(sources) or
            {report.get("filename") for report in reports if isinstance(report, dict)} != set(sources)):
            raise EngineUnavailable("Cloud Run reports 缺少題目或重複；無法採用報告。")
        for report in reports:
            name = report["filename"]
            if report.get("seed") != SEED:
                raise EngineUnavailable("Cloud Run report seed mismatch")
            source_hash = hashlib.sha256(sources[name].encode()).hexdigest()
            if report.get("source_sha256") not in (None, source_hash):
                raise EngineUnavailable("Cloud Run source hash mismatch")
            if report.get("status") in ("PASS", "REVIEW"):
                functional = report.get("functional", {})
                if (report.get("source_sha256") != source_hash or functional.get("completed") != plan[name] or
                    functional.get("failure_count") != 0 or functional.get("aborted") or
                    not report.get("static", {}).get("machine_checked")):
                    raise EngineUnavailable("Cloud Run PASS 報告缺少完整動態／機器碼測試。")
                if name == "p3_2.s" and (functional.get("helper_direct", {}).get("completed") != 256 or
                                        not functional.get("helper_abi_probe")):
                    raise EngineUnavailable("Cloud Run AES helper checks 缺少")
            report["planned_cases"] = payload["plan"][name]
            report["remote_request_id"] = payload.get("request_id")
            report["remote_elapsed_seconds"] = payload.get("elapsed_seconds")
            report["remote_core_sha256"] = payload.get("core_sha256")
        return reports


    async def submit(self, payload: dict[str, Any], sources: dict[str, str],
                     progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None) -> list[dict[str, Any]]:
        import aiohttp
        self.configured()
        if self.closed:
            raise EngineUnavailable("Cloud Run client 已關閉")
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()
        if len(body) > 3 * 1024 * 1024:
            raise GraderError("Cloud Run request exceeds 3 MiB")
        async with self.lock:
            if self.session is None:
                self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=330, connect=20),
                    trust_env=False, raise_for_status=False)
            task = asyncio.create_task(self.heartbeat(progress, time.monotonic()))
            sent_request = False
            try:
                for attempt in range(2):
                    while True:
                        now = time.monotonic()
                        while self.request_times and self.request_times[0] <= now - 61:
                            self.request_times.popleft()
                        if len(self.request_times) < self.rate:
                            break
                        await asyncio.sleep(min(30, max(0.1, 61 - (now - self.request_times[0]))))
                    headers = {"Content-Type": "application/json",
                               **await self.auth_headers(force_refresh=attempt == 1)}
                    self.request_times.append(time.monotonic())
                    sent_request = True
                    async with self.session.post(self.url + "/grade", data=body, headers=headers,
                                                 allow_redirects=False) as response:
                        data = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            data.extend(chunk)
                            if len(data) > MAX_RESPONSE:
                                raise EngineUnavailable("Cloud Run response exceeds 32 MiB; cannot safely use this report")
                        self.request_times[-1] = time.monotonic()
                        if response.status == 401:
                            self.token_until = 0
                            if attempt == 0:
                                # Authentication failed before grading; retry once with a new token.
                                continue
                            raise EngineUnavailable("Cloud Run HTTP 401：ID Token 認證失敗；請檢查金鑰與系統時間。")
                        if response.status == 403:
                            raise EngineUnavailable("Cloud Run HTTP 403：請檢查 roles/run.invoker、API invoker allowlist 與 audience。")
                        if response.status == 429:
                            retry = response.headers.get("Retry-After", "30")
                            seconds = min(300, int(retry)) if retry.isdigit() else 30
                            raise EngineUnavailable(f"Cloud Run HTTP 429：請求過多，請約 {seconds} 秒後重新上傳；尚未完成評分。")
                        if response.status in (503, 504):
                            raise EngineUnavailable(f"Cloud Run HTTP {response.status}：測試服務忙碌或逾時；請稍後重新上傳。")
                        if response.status != 200:
                            error_class = GraderError if response.status == 400 else EngineUnavailable
                            raise error_class(f"Cloud Run HTTP {response.status}：請求未成功；未推估分數。")
                        try:
                            result = json.loads(data)
                        except (ValueError, UnicodeError):
                            raise EngineUnavailable("Cloud Run 回傳格式無法辨識；請檢查部署版本。") from None
                        return self.check_response(result, sources)
                raise EngineUnavailable("Cloud Run 認證未完成")
            except (aiohttp.ClientError, asyncio.TimeoutError):
                raise EngineUnavailable("Cloud Run HTTPS 連線失敗／逾時；測試未完成，請稍後重新上傳。") from None
            finally:
                if sent_request:
                    self.request_times[-1] = time.monotonic()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def grade(self, filename: str, source: str,
                    progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None) -> dict[str, Any]:
        normalized = validate_source(source.encode())
        return (await self.submit({"filename": filename, "source": normalized}, {filename: normalized}, progress))[0]

    async def grade_archive(self, filename: str, raw: bytes, bundle: ArchiveBundle,
                            progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None) -> list[dict[str, Any]]:
        return await self.submit({"filename": filename, "archive_base64": base64.b64encode(raw).decode()},
                                 bundle.sources, progress)

    async def close(self) -> None:
        self.closed = True
        self.token = ""
        self.token_until = 0
        self.credentials = None
        if self.session is not None:
            await self.session.close()
            self.session = None


async def environment_check() -> dict[str, Any]:
    """Safe live diagnosis: never prints URLs, file paths, keys or tokens."""
    import aiohttp
    engine = RemoteEngine()
    result: dict[str, Any] = {"status": "UNAVAILABLE", "operating_system": platform.system(),
        "cloud_run_url_configured": bool(engine.url), "url_setting_source": engine.url_source,
        "credentials_path_configured": engine.credentials_path is not None,
        "credentials_setting_source": engine.path_source, "credentials_file_exists": False,
        "credentials_file_readable": False, "id_token_acquired": False,
        "private_cloud_run_connected": False, "grading_executed": False, "auth_mode": "private IAM",
        "iam_policy_checked": False, "iam_policy_note": "HTTP 診斷不讀取 IAM policy；部署腳本另行只讀核對私人設定。"}
    try:
        engine.configured()
        assert engine.credentials_path is not None
        def file_flags() -> tuple[bool, bool]:
            try:
                exists = engine.credentials_path.is_file()
                with engine.credentials_path.open("rb") as stream:
                    stream.read(1)
                return exists, True
            except OSError:
                return engine.credentials_path.exists(), False
        result["credentials_file_exists"], result["credentials_file_readable"] = await asyncio.to_thread(file_flags)
        headers = await engine.auth_headers()
        result["id_token_acquired"] = True
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40, connect=15), trust_env=False) as session:
            async with session.get(engine.url + "/health", allow_redirects=False) as response:
                anonymous_status = response.status
                await response.content.read(8192)
            result["anonymous_request_denied"] = anonymous_status in (401, 403)
            if not result["anonymous_request_denied"]:
                raise EngineUnavailable("服務未拒絕匿名探測；請確認 Cloud Run 維持私人 IAM，本診斷不會修改權限。")
            # Valid IAM + deliberately missing submission must be rejected before a worker starts.
            async with session.post(engine.url + "/grade", data=b"{}",
                                    headers={"Content-Type": "application/json", **headers},
                                    allow_redirects=False) as response:
                status = response.status
                raw = await response.content.read(8192)
            result["authenticated_http_status"] = status
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeError):
                payload = {}
            if status == 400 and payload.get("not_started") is True:
                result.update(status="READY", private_cloud_run_connected=True)
            elif status in (401, 403):
                raise EngineUnavailable(f"私人 Cloud Run HTTP {status}：請檢查 invoker 權限、audience 與 API allowlist。")
            elif status == 429:
                raise EngineUnavailable("私人 Cloud Run HTTP 429：已限流，請稍後再診斷。")
            else:
                raise EngineUnavailable("私人 Cloud Run 連線／API 驗證尚未成功；沒有執行 Assembly。")
    except EngineUnavailable as exc:
        result["error"] = str(exc)
    except Exception:
        result["error"] = "安全診斷未完成：請檢查設定、Python dependencies、檔案權限與 HTTPS 網路。"
    finally:
        await engine.close()
    return result

async def cli_grade(path: str) -> None:
    file = Path(path)
    kind = attachment_kind(file.name)
    if kind is None:
        raise SubmissionFormatError("只接受指定四份 .s 或 <studentID>_PA1.zip；未讀取此檔案。")
    engine = RemoteEngine()
    try:
        if kind == "archive":
            raw = file.read_bytes()
            bundle = unpack_archive(raw, file.name)
            result: Any = await engine.grade_archive(file.name, raw, bundle)
        else:
            result = await engine.grade(file.name, validate_source(file.read_bytes()))
        print(json.dumps(public_report(result), ensure_ascii=False, indent=2))
    finally:
        await engine.close()

async def engine_self_test() -> dict[str, Any]:
    engine = RemoteEngine()
    try:
        reports = {}
        for name, source in trusted_reference_sources().items():
            report = await engine.grade(name, source)
            if report.get("functional", {}).get("failure_count") != 0 or report["status"] not in ("PASS", "REVIEW"):
                raise GraderError(f"Remote trusted fixture failed: {name}")
            reports[name] = {"status": report["status"], "groups": report["functional"]["groups"]}
        review = await engine.grade("p3_2.s", stack_load_reference_sources()["p3_2.s"])
        if (review["status"] != "REVIEW" or review["functional"]["failure_count"] != 0 or
            review["static"].get("zero_lut_literal")):
            raise GraderError("Remote stack-load comparison fixture did not produce strict REVIEW")
        reports["p3_2_stack_load"] = {"status": review["status"], "groups": review["functional"]["groups"]}
        if reports["p3_2.s"]["status"] != "PASS":
            raise GraderError("Remote load-free AES reference did not PASS")
        return {"status": "PASS", "reports": reports}
    finally:
        await engine.close()

import discord

from discord.ext import commands


def public_text(value: Any) -> str:
    """Redact authentication material and private host paths from user-facing text."""
    text = str(value)
    patterns = (
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        r"(?i)\bBearer\s+[A-Za-z0-9_.~+/=-]+",
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
        r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
        r"\bAIza[0-9A-Za-z_-]{35}\b",
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b",
        r"\b[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,45}\b",
        r"(?i)\b[A-Z]:[\\/]+Users[\\/]+[^\r\n\"'<>]+",
        r"/home/[^\s\"'<>]+",
        r"/tmp/pa1-[^\s\"'<>]+",
        r"(?i)(?:api[_ -]?key|client[_ -]?secret|password|access[_ -]?token|id[_ -]?token)\s*[:=]\s*[\"']?[A-Za-z0-9_./+=-]{12,}",
    )
    for pattern in patterns:
        text = re.sub(pattern, "[已遮罩]", text)
    return text


def public_report(value: Any) -> Any:
    """Keep test records while excluding credentials and host-only metadata."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            sensitive = (normalized in {"authorization", "headers", "request_headers", "cookies", "cookie",
                "credentials", "credentials_path", "credentials_file", "private_key", "private_key_id",
                "client_secret", "password", "passwd", "api_key", "id_token", "access_token", "refresh_token",
                "bearer_token", "token", "bot_token", "discord_token", "secret_key", "secret", "aws_access_key_id", "aws_secret_access_key", "aws_session_token",
                "environment", "env", "server_path", "server_ip", "hostname", "cwd"}
                or normalized.endswith(("_secret", "_password", "_credentials_path")))
            result[str(key)] = "[已遮罩]" if sensitive else public_report(item)
        return result
    if isinstance(value, (list, tuple)):
        return [public_report(item) for item in value]
    return public_text(value) if isinstance(value, str) else value


def clipped(text: Any, maximum: int = 1000) -> str:
    value = public_text(text).replace(chr(96) * 3, chr(96) + "\u200b" + chr(96) * 2).replace("@", "@\u200b")
    return value if len(value) <= maximum else value[:maximum - 16] + "…（見完整報告）"


def report_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(public_report(payload), ensure_ascii=True, indent=2).encode("utf-8")


PROBLEM_NAMES = {"p2.s": "P2 · BST 區間總和", "p3_1.s": "P3-1 · GF(2⁸) 乘法",
                 "p3_2.s": "P3-2 · AES S-Box", "p4.s": "P4 · 字元頻率統計"}
STATUS_COLORS = {"PASS": 0x2ECC71, "FAIL": 0xE74C3C, "REVIEW": 0xF1C40F, "UNAVAILABLE": 0x95A5A6,
                 "FORMAT_ERROR": 0xE74C3C}
STATUS_ICONS = {"PASS": "✅", "FAIL": "❌", "REVIEW": "⚠️", "UNAVAILABLE": "⬜"}
DISCLAIMER = "依 PDF 規則推估，非官方成績；本機測資比例不等於官方分數。P1 尚未人工評分。"
GROUP_NAMES = {"Public examples": "公開範例", "Edge cases": "邊界測試", "Random / Stress": "隨機與壓力測試",
               "Repeated calls": "連續呼叫測試", "Exhaustive 256 x 256": "全部 GF 乘法組合", "Exhaustive 256": "全部 byte 輸入"}


def display_status(report: dict[str, Any]) -> str:
    status = report.get("status", "UNAVAILABLE")
    return "REVIEW" if status == "Compatibility Unverified" else "FAIL" if status == "ERROR" else status if status in STATUS_COLORS else "UNAVAILABLE"


def readable_status(value: Any) -> str:
    name = str(value)
    return {"PASS": "通過", "FAIL": "未通過", "REVIEW": "待確認", "UNAVAILABLE": "無法確認", "UNVERIFIED": "尚未驗證",
            "NOT RUN": "尚未執行", "INCOMPLETE": "未完成", "N/A": "不適用",
            "FAIL (literal rule)": "未符合字面規則", "See failures": "請查看失敗原因"}.get(name, "需查看進階資訊")


def test_count(report: dict[str, Any]) -> str:
    f = report.get("functional", {})
    if "passed" not in f or "completed" not in f:
        return "尚無完整結果"
    total = report.get("planned_cases", f["completed"])
    suffix = f" · 已執行 {f['completed']:,}（未完成）" if f.get("aborted") else ""
    return f"{f['passed']:,} / {total:,}" + suffix


def score_states(report: dict[str, Any]) -> dict[str, Any]:
    """Separate observed function/ISA evidence from conditional official eligibility."""
    e, f, s = report.get("eligibility", {}), report.get("functional", {}), report.get("static", {})
    complete = (bool(f.get("completed")) and not f.get("aborted") and
                f.get("completed") == report.get("planned_cases", f.get("completed")))
    all_pass = complete and f.get("failure_count") == 0
    unavailable = not complete or display_status(report) == "UNAVAILABLE" or report.get("status") == "Compatibility Unverified"
    basic = "UNAVAILABLE" if unavailable or not e else (
        "FAIL" if not all_pass else "PASS" if e.get("basic") == "Eligible estimate" else "REVIEW")
    isa = "UNAVAILABLE" if not s.get("machine_checked") else "PASS" if s.get("rv32i") else "FAIL"
    maximum = e.get("bonus_max")
    bonus = "N/A" if maximum == 0 else "UNAVAILABLE" if maximum is None or isa == "UNAVAILABLE" else (
        "FAIL" if isa == "FAIL" else "UNAVAILABLE" if unavailable else "FAIL" if not all_pass else
        "REVIEW" if report["filename"] == "p3_1.s" or basic == "REVIEW" else "PASS")
    return {"basic": basic, "bonus": bonus, "isa": isa,
            "algorithm": "REVIEW" if report["filename"] == "p3_1.s" else "N/A"}


def eligibility_text(report: dict[str, Any]) -> str:
    e = report.get("eligibility")
    if not e:
        return "基本分／Bonus：尚未推估"
    state = score_states(report)
    base, bonus = e["basic_max"], e["bonus_max"]
    if state["basic"] == "PASS":
        text = f"基本分：**{base} / {base}（預估符合）**"
    elif state["basic"] == "REVIEW":
        text = f"基本分：**待確認**（本題上限 {base} 分）"
    elif state["basic"] == "FAIL":
        text = f"基本分：**待官方評分**（本機測試未全通過；上限 {base} 分）"
    else:
        text = f"基本分：**無法推估**（本題上限 {base} 分）"
    b = state["bonus"]
    text += "\nBonus：" + ("**N/A（本題無加分）**" if b == "N/A" else
        f"**+{bonus}（預估符合）**" if b == "PASS" else f"**+{bonus}（待確認）**" if b == "REVIEW" else
        "**+0（不符合 RV32I 條件）**" if b == "FAIL" and state["isa"] == "FAIL" else
        "**+0（本機功能測試未全通過）**" if b == "FAIL" else "**尚無完整結果**")
    return text


def component_result(report: dict[str, Any]) -> dict[str, Any]:
    if report.get("assessment"):
        return report["assessment"]
    f, s = report.get("functional", {}), report.get("static", {})
    functional_status = ("NOT RUN" if not f else "INCOMPLETE" if f.get("aborted") else
                         "PASS" if f.get("failure_count") == 0 else "FAIL")
    return {"functional_correctness": functional_status, "abi": f.get("memory_abi", "UNVERIFIED"),
            "memory": f.get("memory_abi", "UNVERIFIED"), "zero_lut_compliance": "PASS" if s.get("zero_lut_literal") else
            "FAIL (literal rule)" if s.get("machine_checked") else "UNVERIFIED",
            "lookup_table_detection": f.get("lookup_access", "UNVERIFIED"), "manual_review": s.get("scope_risks", [])}


def review_reasons(report: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    state, s, f = score_states(report), report.get("static", {}), report.get("functional", {})
    if state["bonus"] == "REVIEW" and report["filename"] == "p3_1.s":
        reasons.append("未使用 RV32M；指定的 8 次 Shift-and-XOR 演算法形式仍待確認。")
    if report["filename"] == "p3_2.s" and s.get("machine_checked") and not s.get("zero_lut_literal"):
        reasons.append("Zero-LUT 字面規則發現 load／資料位址指令；stack load 是否例外需由助教確認。")
    if f.get("helper_missing_nonzero") or (f.get("helper_direct") or {}).get("failed"):
        reasons.append("gf28_mul 的呼叫或 helper 結果需確認。")
    if s.get("scope_risks"):
        reasons.append("官方擷取 routine 後的相容性有疑義；請查看進階資訊。")
    if report.get("status") == "Compatibility Unverified":
        reasons.append("測試引擎尚無法確認與 Jupiter 的相容性，不推估官方得分。")
    return reasons


def engine_text(report: dict[str, Any]) -> str:
    engine = report.get("engine", "")
    return "Jupiter 3.1 + RISC-V 模擬器" if "Jupiter 3.1" in engine else (
        "其他 RISC-V 引擎（相容性需確認）" if engine else "尚未執行")


def elapsed_text(report: dict[str, Any]) -> str:
    value = report.get("elapsed_seconds", report.get("remote_elapsed_seconds"))
    return f"{value:.2f} 秒" if isinstance(value, (int, float)) else "尚未回傳"


def failure_embed(report: dict[str, Any], failure: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(title=clipped("失敗測資 · " + str(failure.get("test_id", "未知")), 250),
        description=clipped(PROBLEM_NAMES.get(report["filename"], report["filename"]), 300), color=STATUS_COLORS["FAIL"])
    fence = chr(96) * 3
    for title, key, visible in (("輸入（Input）", "input", "input_visible"),
                                ("預期（Expected）", "expected", "expected_visible"), ("實際（Actual）", "actual", "actual_visible")):
        value = failure.get(visible)
        if value is None:
            value = json.dumps(failure.get(key), ensure_ascii=True)
        embed.add_field(name=title, value=fence + "text\n" + clipped(value, 880) + "\n" + fence, inline=False)
    kind = failure.get("error_type", "execution_error")
    explanation = {"output_mismatch": "輸出不符預期", "instruction_limit": "超過指令上限，可能有無窮迴圈",
        "memory_access": "非法記憶體存取", "calling_convention": "暫存器／stack 未正確還原",
        "return_or_control_flow": "返回位址或跳躍錯誤", "execution_error": "程式執行錯誤"}.get(kind, "請查看完整紀錄")
    embed.add_field(name="錯誤種類", value=clipped(f"{explanation}（{kind}）", 500), inline=False)
    embed.add_field(name="重現", value=clipped(f"Seed {failure.get('seed', report.get('seed', '未知'))} · case index {failure.get('case_index', '未知')}\n"
        f"Source SHA256：{report.get('source_sha256', '未提供')}\n<SPACE> 表示空白，<NUL> 表示終止符；完整 BST／buffer 與所有失敗結果保留於下載報告。", 500), inline=False)
    embed.set_footer(text=DISCLAIMER)
    return embed


def format_error_embed(report: dict[str, Any]) -> discord.Embed:
    archive = report.get("format_kind") == "archive"
    label = "繳交格式錯誤" if archive else "檔案格式錯誤"
    embed = discord.Embed(title="RISC-V PA1 — " + label,
        description=clipped(report["filename"], 160) + "\n未執行 RISC-V 計算測試；此結果不代表程式答案錯誤。",
        color=STATUS_COLORS["FORMAT_ERROR"])
    embed.add_field(name="附件驗證原因", value=clipped(report.get("error", "附件無法驗證"), 950), inline=False)
    advice = ("使用 <studentID>_PA1.zip，內含同名頂層資料夾與四份指定 .s；不得附加其他檔案。"
              if archive else "請上傳符合大小限制的 UTF-8 Assembly 文字，保留正式檔名。")
    embed.add_field(name="修正方式", value=advice, inline=False)
    embed.set_footer(text="檔案／繳交格式檢查，不是官方評分；本次未推估分數。")
    return embed


def summary_embed(report: dict[str, Any]) -> discord.Embed:
    status = display_status(report)
    if status == "FORMAT_ERROR":
        return format_error_embed(report)
    embed = discord.Embed(title="RISC-V PA1 — " + PROBLEM_NAMES.get(report["filename"], report["filename"]),
        description=f"**{status}** — {test_count(report)}", color=STATUS_COLORS[status])
    embed.add_field(name="分數資格（非官方）", value=eligibility_text(report), inline=False)
    reasons = review_reasons(report)
    if reasons:
        embed.add_field(name="待確認事項", value=clipped("\n".join(reasons), 600), inline=False)
    f = report.get("functional", {})
    if f.get("failures"):
        embed.add_field(name="首筆失敗", value=clipped(str(f["failures"][0].get("test_id", "未知")) +
            "；按「查看失敗測資」查看輸入與預期結果。", 250), inline=False)
    if report.get("error"):
        embed.add_field(name="無法完成的原因", value=clipped(report["error"], 500), inline=False)
    embed.add_field(name="測試資訊", value=f"{engine_text(report)} · {elapsed_text(report)}\n分類與規格檢查請按「查看詳細結果」。", inline=False)
    embed.set_footer(text=DISCLAIMER)
    return embed


def result_embed(report: dict[str, Any]) -> discord.Embed:
    status, f, s = display_status(report), report.get("functional", {}), report.get("static", {})
    if status == "FORMAT_ERROR":
        return format_error_embed(report)
    a, state = component_result(report), score_states(report)
    embed = discord.Embed(title=PROBLEM_NAMES.get(report["filename"], report["filename"]) + " · 詳細結果",
        description=f"**{status}** — {test_count(report)}", color=STATUS_COLORS[status])
    if f:
        groups = "\n".join(f"{GROUP_NAMES.get(group, group)}：**{stats['passed']:,} / {stats['total']:,}**"
            for group, stats in f.get("groups", {}).items())
        if f.get("aborted"):
            groups += "\n測試尚未完成；中止原因見進階資訊。"
        embed.add_field(name="功能測試", value=clipped(groups or "尚無分類結果", 900), inline=False)
    isa = "RV32I" if state["isa"] == "PASS" else "含 RV32M／其他 extension" if state["isa"] == "FAIL" else "尚未驗證機器碼"
    m = [item for item in s.get("machine_findings", []) if item.get("kind") == "RV32M"]
    forbidden = "已發現（行號與機器碼見進階資訊）" if m else "未發現 RV32M" if s.get("machine_checked") else "尚未驗證"
    embed.add_field(name="程式規格檢查", value=f"暫存器與函式返回：{readable_status(a.get('abi'))}\n"
        f"記憶體存取：{readable_status(a.get('memory'))}\n指令集：{isa}\n乘除法指令：{forbidden}\n"
        "ABI 要求：還原 sp／ra 與保存暫存器。", inline=False)
    if report["filename"] == "p3_2.s":
        loads = f.get("load_summary", {})
        lookup = "已觀察到非 stack 讀取；是否資料表需確認" if loads.get("non_stack") else (
            "無禁止 load／資料標籤位址指令" if s.get("zero_lut_literal") else
            "本機測資未發現非 stack 讀取；stack load 仍觸犯字面規則" if f else "尚未驗證")
        helper = f.get("helper_direct")
        text = f"功能：{readable_status(a.get('functional_correctness'))}\nZero-LUT：{readable_status(a.get('zero_lut_compliance'))}\n"
        text += f"資料表偵測：{lookup}\nZero-LUT 要求：不得使用 load 或 la 讀取資料標籤。"
        if helper:
            text += f"\ngf28_mul 檢查：{helper['completed'] - helper['failed']} / {helper['completed']}"
        embed.add_field(name="AES／Zero-LUT 檢查", value=clipped(text, 800), inline=False)
    value = eligibility_text(report)
    e = report.get("eligibility", {})
    if state["basic"] == "PASS":
        value += f"\n本題預估資格上限：{e['basic_max'] + (e['bonus_max'] if state['bonus'] == 'PASS' else 0)} 分"
        if state["bonus"] == "REVIEW":
            value += f"；含待確認加分最高 {e['basic_max'] + e['bonus_max']} 分。"
    if state["bonus"] != "N/A":
        value += f"\nRV32I 指令檢查：{readable_status(state['isa'])}（以實際機器碼檢查）"
    embed.add_field(name="基本分與 Bonus 資格", value=clipped(value, 800), inline=False)
    if report["filename"] == "p3_1.s":
        embed.add_field(name="演算法形式（REVIEW）", value="指定的 8 次 Shift-and-XOR 需人工確認。\n"
            "PDF 第 5 頁要求此演算法；第 7 頁評分表寫禁用 RV32M 且官方測資全通過可得加分，是否另查演算法形式尚未確認。"
            "完整功能測試通過仍不能保證官方 Bonus。", inline=False)
    reasons = [r for r in review_reasons(report) if not (report["filename"] == "p3_1.s" and "Shift-and-XOR" in r)]
    if reasons:
        embed.add_field(name="需人工確認", value=clipped("\n".join(reasons), 500), inline=False)
    embed.add_field(name="測試資訊", value=f"{engine_text(report)}\n執行時間：{elapsed_text(report)} · Seed {report.get('seed', '未提供')}", inline=False)
    embed.set_footer(text=DISCLAIMER)
    return embed


def advanced_embeds(report: dict[str, Any]) -> list[discord.Embed]:
    status = display_status(report)
    embed = discord.Embed(title=PROBLEM_NAMES.get(report["filename"], report["filename"]) + " · 進階資訊",
        description="供除錯使用；底層紀錄與全部失敗結果保留於下載報告。", color=STATUS_COLORS[status])
    s, f = report.get("static", {}), report.get("functional", {})
    findings = s.get("machine_findings", []) + s.get("source_findings", [])
    rows = [f"L{x.get('line', '?')} · {x.get('instruction', '?')} · {x.get('kind', '?')} · {x.get('encoding', '來源檢查')}" for x in findings]
    embed.add_field(name="指令與行號", value=clipped("\n".join(rows[:12]) or "未發現違規指令／尚無機器碼紀錄", 950), inline=False)
    risks = s.get("scope_risks", [])
    if risks:
        embed.add_field(name="官方擷取與相容性", value=clipped("\n".join(risks), 650), inline=False)
    probe = f.get("helper_abi_probe")
    if probe and probe.get("failed"):
        embed.add_field(name="Helper 替換診斷（不新增扣分）", value=clipped(f"替換 helper 的 caller-clobber probe：{probe['failed']} 次失敗。\n"
            "固定 helper 可能有額外暫存器保留約定；此探測不等同原程式功能失敗。PDF 對跨函式值保存的文字仍需人工確認。", 600), inline=False)
    if report.get("error") or f.get("aborted"):
        embed.add_field(name="執行錯誤", value=clipped(report.get("error") or f.get("aborted"), 700), inline=False)
    if f.get("failures"):
        embed.add_field(name="原始錯誤", value=clipped(f["failures"][0].get("error", "未提供"), 650), inline=False)
    embed.add_field(name="引擎與重現", value=clipped(f"{report.get('engine', '尚未執行')}\nSource SHA256：{report.get('source_sha256', '未提供')}\n"
        f"Core SHA256：{report.get('remote_core_sha256', '未提供')}\nAPI 記錄指令數：{f.get('instruction_count', '未提供')}", 700), inline=False)
    embed.set_footer(text=DISCLAIMER)
    return [embed]


def submission_embed(reports: list[dict[str, Any]], archives: list[dict[str, Any]],
                     pdfs: list[dict[str, Any]] | None = None) -> discord.Embed:
    """PDF metadata is no longer inspected; legacy argument is inert."""
    statuses = [display_status(r) for r in reports]
    status = next((s for s in ("FAIL", "FORMAT_ERROR", "UNAVAILABLE", "REVIEW", "PASS") if s in statuses), "UNAVAILABLE")
    label = "繳交格式錯誤" if status == "FORMAT_ERROR" else status
    context = "格式錯誤附件未執行計算測試；其他附件獨立處理。" if "FORMAT_ERROR" in statuses else "本機額外測試"
    embed = discord.Embed(title="RISC-V PA1 — 作業測試報告", description=f"**{label}** · {context}",
                          color=STATUS_COLORS[status])
    for report in reports[:8]:
        value = ("**" + ("繳交格式錯誤" if report.get("format_kind") == "archive" else "檔案格式錯誤") +
                 "** · 未執行 RISC-V 測試\n" + clipped(report.get("error", "附件無法驗證"), 240)
                 if report.get("status") == "FORMAT_ERROR" else
                 f"**{display_status(report)}** — {test_count(report)}\n{eligibility_text(report)}")
        embed.add_field(name=clipped(PROBLEM_NAMES.get(report["filename"], report["filename"]), 100),
            value=clipped(value, 380), inline=False)
    if len(reports) > 8:
        embed.add_field(name="其他附件", value=f"另有 {len(reports) - 8} 份結果，請從詳細結果分頁或完整報告查看。", inline=False)
    archive_text = "\n".join(f"{a['filename']}：" + ("繳交格式錯誤" if a.get('structure') == 'FORMAT_ERROR' else a.get('structure', 'PASS'))
                             for a in archives) if archives else "本次為單題／多附件測試；正式繳交另需準備來源 ZIP。"
    pdf_text = "P1 PDF 由既有功能獨立處理；本工具不檢查 PDF，P1 尚未人工評分。"
    embed.add_field(name="繳交提醒（與功能結果分開）", value=clipped(archive_text + "\n" + pdf_text, 650), inline=False)
    issues = list(dict.fromkeys(reason for r in reports for reason in review_reasons(r)))
    if issues:
        embed.add_field(name="待確認事項", value=clipped("\n".join(issues[:3]), 650), inline=False)
    if any(r.get("functional", {}).get("failures") for r in reports):
        embed.add_field(name="失敗說明", value="按「查看失敗測資」查看輸入、預期、實際結果與重現資訊。", inline=False)
    embed.set_footer(text=DISCLAIMER)
    return embed


def loading_embed(names: list[str], stage: str, elapsed: int | None = None) -> discord.Embed:
    embed = discord.Embed(title="RISC-V PA1 — 測試進行中", description=clipped("、".join(names), 800),
                          color=0x5865F2)
    stage = stage.replace("Cloud Run 批次 Testing", "遠端批次測試").replace("Cloud Run Testing", "遠端測試")
    stage = stage.replace("Testing", "測試中").replace("Loading", "載入中")
    embed.add_field(name="目前狀態", value=clipped(stage + (f" · 已等候 {elapsed} 秒" if elapsed is not None else ""), 800), inline=False)
    embed.set_footer(text="完成後更新此訊息；完整報告僅在按下下載按鈕時提供。")
    return embed


class ReportPages(discord.ui.View):
    def __init__(self, owner_id: int, pages: list[discord.Embed],
                 advanced: list[discord.Embed] | None = None) -> None:
        super().__init__(timeout=180)
        self.owner_id, self.pages, self.index = owner_id, list(pages), 0
        self.main_pages = list(pages)
        self.advanced_pages = list(advanced or [])
        self.showing_advanced = False
        self.advanced_button.disabled = not self.advanced_pages
        self.message: Any = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("此報告按鈕僅供原上傳者使用。", ephemeral=True)
        return False

    async def move(self, interaction: discord.Interaction, offset: int) -> None:
        self.index = (self.index + offset) % len(self.pages)
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="上一頁", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.move(interaction, -1)

    @discord.ui.button(label="下一頁", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.move(interaction, 1)

    @discord.ui.button(label="進階資訊", style=discord.ButtonStyle.secondary)
    async def advanced_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.showing_advanced = not self.showing_advanced
        self.pages = self.advanced_pages if self.showing_advanced else self.main_pages
        self.index = 0
        button.label = "返回詳細結果" if self.showing_advanced else "進階資訊"
        await interaction.response.edit_message(embed=self.pages[0], view=self)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                LOG.warning("PA1 detail-view timeout edit failed")

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item[Any]) -> None:
        LOG.warning("PA1 detail interaction failed: %s", type(error).__name__)
        if not interaction.response.is_done():
            await interaction.response.send_message("報告互動暫時無法使用；請重試，或使用下載完整報告按鈕。", ephemeral=True)

class ReportView(discord.ui.View):
    def __init__(self, owner_id: int, reports: list[dict[str, Any]], data: dict[str, Any], filename: str,
                 release: Callable[["ReportView"], None], file_limit: int = MAX_REPORT) -> None:
        super().__init__(timeout=900)
        self.owner_id, self.report_data, self.filename, self.release = owner_id, data, filename, release
        self.details = [result_embed(r) for r in reports]
        self.advanced = [page for report in reports for page in advanced_embeds(report)]
        self.file_limit = file_limit
        self.failures = [failure_embed(r, f) for r in reports for f in r.get("functional", {}).get("failures", [])[:3]]
        self.message: Any = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("此報告按鈕僅供原上傳者使用。", ephemeral=True)
        return False

    async def pages(self, interaction: discord.Interaction, pages: list[discord.Embed],
                    advanced: list[discord.Embed] | None = None) -> None:
        if not pages:
            await interaction.response.send_message("沒有可顯示的失敗測資；若測試未完成，請查看 UNAVAILABLE 原因。", ephemeral=True)
            return
        view = ReportPages(self.owner_id, pages, advanced)
        await interaction.response.send_message(embed=pages[0], view=view, ephemeral=True,
                                                 allowed_mentions=discord.AllowedMentions.none())
        view.message = await interaction.original_response()

    @discord.ui.button(label="查看詳細結果", style=discord.ButtonStyle.primary, emoji="📊")
    async def details_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.pages(interaction, self.details, self.advanced)

    @discord.ui.button(label="查看失敗測資", style=discord.ButtonStyle.secondary, emoji="🔎")
    async def failure_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.pages(interaction, self.failures)

    @discord.ui.button(label="下載完整報告", style=discord.ButtonStyle.secondary, emoji="📥")
    async def download_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.report_data:
            await interaction.response.send_message("互動報告已過期；請重新上傳檔案以產生新報告。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await asyncio.to_thread(report_bytes, self.report_data)
        filename = self.filename
        if len(data) > self.file_limit:
            import gzip
            data = await asyncio.to_thread(gzip.compress, data)
            filename += ".gz"
        if len(data) > self.file_limit:
            await interaction.followup.send("完整報告壓縮後仍超過附件限制；請分題重新上傳。", ephemeral=True)
            return
        attached = discord.File(io.BytesIO(data), filename=filename)
        try:
            await interaction.followup.send(file=attached, ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())
        finally:
            attached.close()

    def dispose(self) -> None:
        self.report_data = {}
        self.details.clear()
        self.advanced.clear()
        self.failures.clear()
        self.stop()
        self.release(self)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                LOG.warning("PA1 report-view timeout edit failed")
        self.dispose()

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item[Any]) -> None:
        LOG.warning("PA1 report interaction failed: %s", type(error).__name__)
        if not interaction.response.is_done():
            await interaction.response.send_message("報告按鈕暫時無法使用；請重試或重新上傳。", ephemeral=True)
        else:
            await interaction.followup.send("報告按鈕暫時無法使用；請重試或重新上傳。", ephemeral=True)


@dataclass
class UploadJob:
    message: discord.Message
    attachments: list[discord.Attachment]
    progress_message: discord.Message
    admitted: asyncio.Event = field(default_factory=asyncio.Event)

class PA1Grader(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot, self.engine = bot, RemoteEngine()
        self.queue: asyncio.Queue[UploadJob] = asyncio.Queue(maxsize=bounded_env("PA1_QUEUE_SIZE", 8, 1, 32))
        self.seen: OrderedDict[tuple[int, int], float] = OrderedDict()
        self.workers: list[asyncio.Task[None]] = []
        self.worker_count = bounded_env("PA1_WORKERS", 1, 1, 4)
        self.accept_lock = asyncio.Lock()
        self.recent_recovery: OrderedDict[int, float] = OrderedDict()
        self.closing = False
        self.report_views: OrderedDict[int, ReportView] = OrderedDict()

    async def cog_load(self) -> None:
        self.workers = [asyncio.create_task(self.work_loop(), name=f"pa1-worker-{i}") for i in range(self.worker_count)]
        if not self.bot.intents.message_content:
            LOG.warning("PA1 needs Message Content intent in code and Developer Portal")

    async def cog_unload(self) -> None:
        self.closing = True
        for task in self.workers: task.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        await self.engine.close()
        for view in list(self.report_views.values()):
            await view.on_timeout()
        while not self.queue.empty():
            job = self.queue.get_nowait()
            await self.safe_edit(job.progress_message, content="PA1 Cog 已卸載；排隊測試已取消。")
            self.queue.task_done()

    async def safe_send(self, channel: Any, **kwargs: Any) -> discord.Message | None:
        try:
            return await channel.send(allowed_mentions=discord.AllowedMentions.none(), **kwargs)
        except discord.HTTPException:
            LOG.warning("PA1 cannot reply in channel/thread %s", getattr(channel, "id", None))
            return None

    async def safe_edit(self, message: discord.Message, **kwargs: Any) -> bool:
        try:
            await message.edit(allowed_mentions=discord.AllowedMentions.none(), **kwargs)
            return True
        except discord.HTTPException:
            LOG.warning("PA1 progress edit failed")
            return False

    def release_report_view(self, view: ReportView) -> None:
        self.report_views.pop(id(view), None)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        try:
            await self.accept_message(message)
        except Exception:
            LOG.warning("PA1 attachment listener failed; other Cogs continue")

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        # Forum starter messages may be emitted before the thread is cached. Recover
        # the starter by its thread ID, then a short history window; attachment IDs
        # deduplicate the corresponding normal message event.
        now = time.monotonic()
        if thread.id in self.recent_recovery:
            return
        self.recent_recovery[thread.id] = now
        while len(self.recent_recovery) > 512: self.recent_recovery.popitem(last=False)
        try:
            if getattr(thread, "created_at", None) and (discord.utils.utcnow() - thread.created_at).total_seconds() > 120:
                return
            for attempt in range(3):
                try:
                    starter = await thread.fetch_message(thread.id)
                    await self.accept_message(starter)
                    return
                except discord.NotFound:
                    if attempt < 2: await asyncio.sleep(1)
            async for message in thread.history(limit=5, oldest_first=True):
                await self.accept_message(message)
        except discord.HTTPException:
            LOG.warning("PA1 cannot read thread starter/history: %s", thread.id)
        except Exception:
            LOG.warning("PA1 thread recovery failed; other Cogs continue")

    async def accept_message(self, message: discord.Message) -> None:
        if self.closing or message.author.bot or getattr(message, "webhook_id", None):
            return
        attachments = [a for a in message.attachments if attachment_kind(a.filename) is not None]
        if not attachments:
            return
        async with self.accept_lock:
            if self.closing:
                return
            now = time.monotonic()
            while self.seen and (len(self.seen) > 4096 or next(iter(self.seen.values())) < now - 86400):
                self.seen.popitem(last=False)
            attachments = [a for a in attachments if (message.id, a.id) not in self.seen]
            if not attachments:
                return
            if self.queue.full():
                await self.safe_send(message.channel, content="PA1 測試佇列已滿，請稍後重新上傳。")
                return
            if message.guild is not None:
                me = message.guild.me
                if me is not None:
                    permissions = message.channel.permissions_for(me)
                    send_ok = permissions.send_messages_in_threads if isinstance(message.channel, discord.Thread) else permissions.send_messages
                    if not permissions.view_channel or not send_ok:
                        LOG.warning("PA1 missing view/send permission in %s", message.channel.id)
                        return
                    if not permissions.embed_links or not permissions.attach_files:
                        await self.safe_send(message.channel, content="PA1 需要 Embed Links 與 Attach Files 權限，請管理員調整此頻道／Thread。")
                        return
            progress_message = await self.safe_send(message.channel, embed=loading_embed([a.filename for a in attachments], "驗證附件／排隊中"))
            if progress_message is None:
                return
            if self.closing:
                await self.safe_edit(progress_message, content="PA1 Cog 已卸載；測試已取消。")
                return
            job = UploadJob(message, attachments, progress_message)
            self.queue.put_nowait(job)
            for attachment in attachments:
                self.seen[(message.id, attachment.id)] = now
            job.admitted.set()

    async def work_loop(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                await job.admitted.wait()
                await self.process_job(job)
            except asyncio.CancelledError:
                await self.safe_edit(job.progress_message, content="PA1 測試因 Cog 卸載而取消。")
                raise
            except Exception:
                LOG.warning("PA1 queued job failed; worker stays available")
                await self.safe_edit(job.progress_message, content="PA1 測試系統發生錯誤；其他 Bot 功能與佇列繼續運作。")
            finally:
                self.queue.task_done()

    async def process_job(self, job: UploadJob) -> None:
        # Recheck admission before any file I/O, even for manually constructed jobs.
        attachments = [a for a in job.attachments if attachment_kind(a.filename) is not None]
        if not attachments:
            return
        reports: list[dict[str, Any]] = []
        archives: list[dict[str, Any]] = []
        last_edit = 0.0

        async def update(payload: dict[str, Any]) -> None:
            nonlocal last_edit
            if time.monotonic() - last_edit >= 15:
                last_edit = time.monotonic()
                await self.safe_edit(job.progress_message, content=None,
                                     embed=loading_embed([attachment.filename], "Cloud Run Testing", payload["elapsed"]))

        for attachment in attachments:
            kind = attachment_kind(attachment.filename)
            report: dict[str, Any] = {"filename": attachment.filename, "seed": SEED, "status": "ERROR"}
            archive_record: dict[str, Any] | None = None
            if kind == "archive":
                archive_record = {"filename": attachment.filename, "structure": "NOT VALIDATED"}
                archives.append(archive_record)
            try:
                limit = MAX_ARCHIVE if kind == "archive" else MAX_SOURCE
                if attachment.size > limit or attachment.size <= 0:
                    raise SubmissionFormatError(f"附件大小需為 1..{limit} bytes")
                raw = await asyncio.wait_for(attachment.read(), timeout=30)
                if not raw or len(raw) > limit:
                    raise SubmissionFormatError(f"附件下載大小需為 1..{limit} bytes")
                if kind == "archive":
                    bundle = await asyncio.to_thread(unpack_archive, raw, attachment.filename)
                    assert archive_record is not None
                    archive_record.update(structure="PASS", folder=bundle.folder, sources=list(bundle.sources))
                    await self.safe_edit(job.progress_message, content=None,
                                         embed=loading_embed(list(bundle.sources), "Cloud Run 批次 Testing"))
                    reports.extend(await self.engine.grade_archive(attachment.filename, raw, bundle, update))
                    continue
                try:
                    source = validate_source(raw)
                except GraderError as exc:
                    raise SubmissionFormatError(str(exc)) from exc
                scope = await asyncio.to_thread(scope_source, source, attachment.filename)
                report["static"] = source_checks(scope, attachment.filename)
                await self.safe_edit(job.progress_message, content=None,
                                     embed=loading_embed([attachment.filename], "Cloud Run Testing"))
                report = await self.engine.grade(attachment.filename, source, update)
            except asyncio.CancelledError:
                raise
            except SubmissionFormatError as exc:
                report.update(status="FORMAT_ERROR", format_kind=kind,
                              error_type="submission_format", error=str(exc))
                if archive_record is not None:
                    archive_record.update(structure="FORMAT_ERROR", error=str(exc))
            except (GraderError, OSError, asyncio.TimeoutError, discord.HTTPException) as exc:
                report["error"] = str(exc) or type(exc).__name__
                if isinstance(exc, EngineUnavailable) or "執行引擎尚未安裝" in report["error"]:
                    report["status"] = "UNAVAILABLE"
            except Exception as exc:
                LOG.warning("PA1 individual attachment failed: %s", type(exc).__name__)
                report["error"] = f"Internal error: {type(exc).__name__}; see Bot log"
            reports.append(report)
        payload = {"reports": reports, "archives": archives,
                   "reproduce": "Use source SHA256, filename, API seed, test_id/case_index and full failure input with the same deployed revision.",
                   "note": "Non-official results. P1 requires manual grading. Expanded inputs capped; all_failure_results preserves every failed case."}
        file_limit = min(MAX_REPORT, getattr(job.message.guild, "filesize_limit", MAX_REPORT))
        while len(self.report_views) >= 8:
            _, expired = self.report_views.popitem(last=False)
            await expired.on_timeout()
        view = ReportView(job.message.author.id, reports, payload, "pa1-report.json", self.release_report_view, file_limit)
        self.report_views[id(view)] = view
        final_embed = (summary_embed(reports[0]) if len(reports) == 1 and not archives
                       else submission_embed(reports, archives))
        updated = await self.safe_edit(job.progress_message, content=None, embeds=[final_embed],
                                       attachments=[], view=view)
        if updated:
            view.message = job.progress_message
        else:
            view.message = await self.safe_send(job.message.channel, embed=final_embed, view=view)
            if view.message is None:
                view.dispose()

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PA1Grader(bot))

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        if sys.argv[1:] == ["--environment-check"]:
            print(json.dumps(asyncio.run(environment_check()), ensure_ascii=False, indent=2))
        elif sys.argv[1:] == ["--engine-self-test"]:
            print(json.dumps(asyncio.run(engine_self_test()), ensure_ascii=False, indent=2))
        elif len(sys.argv) == 3 and sys.argv[1] == "--grade":
            asyncio.run(cli_grade(sys.argv[2]))
        else:
            print(__doc__)
        raise SystemExit(0)
    except (GraderError, OSError, ValueError) as exc:
        print(public_text(exc), file=sys.stderr)
        raise SystemExit(2) from exc


