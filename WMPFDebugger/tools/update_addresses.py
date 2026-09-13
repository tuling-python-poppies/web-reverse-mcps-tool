#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_addresses.py - WMPFDebugger WMPF 地址自动更新器

定位 %APPDATA%\\Tencent\\xwechat\\xplugin\\Plugins\\RadiumWMPF\\<版本>\\extracted\\runtime
下的 flue.dll (旧版本为 WeChatAppEx.exe), 通过与版本无关的锚点字符串 + x64 指令模式
自动提取三个配置项, 并写入 frida/config/addresses.<版本>.json:

    LoadStartHookOffset   OnLoadStart 函数入口 (RVA)
    CDPFilterHookOffset   SendToClientFilter 引用函数的第一个 call 目标 (RVA)
    SceneOffsets          6 元 deref 链偏移 [o0, o1, o2, o3, o4, o5]

依赖: pefile  capstone  (pip install pefile capstone)

用法:
    python tools/update_addresses.py                  # 自动检测最新 WMPF 版本, 干跑并打印
    python tools/update_addresses.py --write          # 自动检测并写入 config
    python tools/update_addresses.py --version 25558  # 指定版本
    python tools/update_addresses.py --pe <path>      # 直接指定 PE 文件
    python tools/update_addresses.py --list           # 列出本机已安装的 WMPF 版本
"""

import argparse
import json
import os
import re
import struct
import sys
import tempfile
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path

try:
    import pefile
except ImportError:
    pefile = None
try:
    from capstone import Cs, CS_ARCH_X86, CS_MODE_64
except ImportError:
    Cs = None

BASE_WMPF_DIRS = [
    Path(os.environ.get("APPDATA", "")) / "Tencent" / "xwechat" / "xplugin" / "Plugins" / "RadiumWMPF",
    Path(os.environ.get("APPDATA", "")) / "Tencent" / "WeChat" / "xwechat" / "xplugin" / "Plugins" / "RadiumWMPF",
]
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "frida" / "config"

ANCHOR_STRINGS = {
    "OnLoadStart": b"OnLoadStart",
    "applet_index_container.cc": b"applet_index_container.cc",
    "AppletIndexContainer::OnLoadStart": b"AppletIndexContainer::OnLoadStart",
    "[perf] AppletIndexContainer::OnLoadStart": b"[perf] AppletIndexContainer::OnLoadStart",
    "SendToClientFilter": b"SendToClientFilter",
    "devtools_message_filter_applet_webview.cc": b"devtools_message_filter_applet_webview.cc",
}

OLD_VERSION_BOUNDARY = 13331

XREF_RE = re.compile(rb"[\x40-\x4f][\x8d\x8b\x89\x8a][\x05\x0d\x15\x1d\x25\x2d\x35\x3d]")

# The real AppletIndexContainer::OnLoadStart keeps the debug flag (dl) in the
# prologue and later branches on it to drive the devtools socket.  RIP
# displacements and source line immediates are intentionally wildcarded.
LOADSTART_SIGNATURE = [
    ("push", r"r15"),
    ("push", r"r14"),
    ("push", r"rsi"),
    ("push", r"rdi"),
    ("push", r"rbx"),
    ("sub", r"rsp, 0x[0-9a-f]+"),
    ("mov", r"ebx, edx"),
    ("mov", r"rsi, rcx"),
    ("cmp", r"dword ptr \[rip \+ 0x[0-9a-f]+\], 2"),
    ("jg", r"0x[0-9a-f]+"),
    ("cmp", r"qword ptr \[rip \+ 0x[0-9a-f]+\], 0"),
    ("je", r"0x[0-9a-f]+"),
    ("lea", r"rdx, \[rip \+ 0x[0-9a-f]+\]"),
    ("lea", r"rcx, \[rsp \+ 0x[0-9a-f]+\]"),
    ("mov", r"r8d, 0x[0-9a-f]+"),
    ("mov", r"r9d, 2"),
    ("call", r"0x[0-9a-f]+"),
    ("lea", r"rcx, \[rsp \+ 0x[0-9a-f]+\]"),
    ("lea", r"rdx, \[rip \+ 0x[0-9a-f]+\]"),
    ("mov", r"r8d, 1"),
    ("call", r"0x[0-9a-f]+"),
    ("mov", r"rdi, rax"),
    ("mov", r"rax, qword ptr \[rsi \+ 0x40\]"),
    ("cmp", r"byte ptr \[rax \+ 0xaf\], 0"),
    ("js", r"0x[0-9a-f]+"),
    ("add", r"rax, 0x98"),
    ("mov", r"rcx, qword ptr \[rax \+ 0x10\]"),
]

MODRM_RIP_MODRM = set(range(0x05, 0x40, 0x08))


@dataclass
class Xref:
    raw_off: int
    va: int
    rva: int
    insn_hex: str


def log(msg: str) -> None:
    print(f"[scanner] {msg}", flush=True)


@dataclass
class PEImage:
    data: bytes
    base: int
    sections: list
    text_va_start: int
    text_va_end: int
    text_raw_start: int
    text_raw_end: int
    sec_list: list = field(default_factory=list)
    fn_ranges: list = field(default_factory=list)
    fn_starts: list = field(default_factory=list)

    @classmethod
    def open(cls, path: Path) -> "PEImage":
        if pefile is None:
            sys.exit("缺少依赖 pefile, 请运行: pip install pefile capstone")
        data = path.read_bytes()
        p = pefile.PE(data=data, fast_load=True)

        secs = []
        for s in p.sections:
            name = s.Name.rstrip(b"\x00").decode(errors="replace")
            rva = s.VirtualAddress
            vsz = s.Misc_VirtualSize
            raw = s.PointerToRawData
            rawsz = s.SizeOfRawData
            secs.append((name, rva, vsz, raw, rawsz))

        text = next((s for s in secs if s[0].startswith(".text")), None)
        if text is None:
            sys.exit("未找到 .text 段, 不支持该 PE")

        img = cls(
            data=data,
            base=p.OPTIONAL_HEADER.ImageBase,
            sections=secs,
            text_va_start=p.OPTIONAL_HEADER.ImageBase + text[1],
            text_va_end=p.OPTIONAL_HEADER.ImageBase + text[1] + text[2],
            text_raw_start=text[3],
            text_raw_end=text[3] + text[4],
            sec_list=list(secs),
        )

        pdata = next((s for s in secs if s[0].startswith(".pdata")), None)
        if pdata is not None:
            img._load_pdata(pdata)
        return img

    def _load_pdata(self, pdata) -> None:
        name, rva, vsz, raw, rawsz = pdata
        n = min(rawsz, max(vsz, 0) + 0x1000) // 12
        n = max(0, n)
        ranges = []
        sz = 12
        for i in range(n):
            off = raw + i * sz
            if off + sz > len(self.data):
                break
            begin, end, _unwind = struct.unpack_from("<III", self.data, off)
            if begin == 0 or end <= begin:
                continue
            if begin < self.text_va_start - self.base and end > self.text_va_start - self.base:
                pass
            ranges.append((begin, end))
        ranges.sort()
        self.fn_ranges = ranges
        self.fn_starts = [r[0] for r in ranges]

    def rva_to_raw(self, rva: int) -> int:
        for name, sva, svsz, raw, rawsz in self.sec_list:
            if sva <= rva < sva + svsz:
                o = rva - sva
                if o < rawsz:
                    return raw + o
                return raw + min(o, rawsz - 1)
        return -1

    def raw_to_rva_off(self, raw_off: int) -> int:
        for name, sva, svsz, raw, rawsz in self.sec_list:
            if raw <= raw_off < raw + rawsz:
                return sva + (raw_off - raw)
        return -1

    def va_from_rva(self, rva: int) -> int:
        return self.base + rva

    def function_by_rva(self, rva: int) -> tuple | None:
        if not self.fn_starts:
            return None
        idx = bisect_right(self.fn_starts, rva) - 1
        if idx < 0:
            return None
        begin, end = self.fn_ranges[idx]
        if begin <= rva < end:
            return (begin, end)
        return None

    def in_text(self, rva: int) -> bool:
        return self.text_raw_start <= self.rva_to_raw(rva) <= self.text_raw_end

    def disasm(self, va: int, size: int):
        if Cs is None:
            sys.exit("缺少依赖 capstone, 请运行: pip install pefile capstone")
        md = Cs(CS_ARCH_X86, CS_MODE_64)
        md.detail = False
        raw = self.rva_to_raw(va - self.base)
        if raw < 0:
            return []
        chunk = self.data[raw: raw + size]
        return list(md.disasm(chunk, va))


def find_loadstart_by_signature(pe: PEImage) -> list[int]:
    """Find functions matching the current WMPF OnLoadStart entry shape."""
    if not pe.fn_starts:
        return []

    wanted = [(mnemonic, re.compile(pattern)) for mnemonic, pattern in LOADSTART_SIGNATURE]
    matches = []
    for start in pe.fn_starts:
        raw = pe.rva_to_raw(start)
        if raw < 0 or pe.data[raw:raw + 10] != b"\x41\x57\x41\x56\x56\x57\x53\x48\x81\xec":
            continue
        insns = pe.disasm(pe.va_from_rva(start), 0x100)
        if len(insns) < len(wanted):
            continue
        if all(
            insns[i].mnemonic == mnemonic and pattern.fullmatch(insns[i].op_str)
            for i, (mnemonic, pattern) in enumerate(wanted)
        ):
            matches.append(start)
    return matches


def find_loadstart_by_relaxed_signature(pe: PEImage) -> list[tuple[int, int]]:
    """Score nearby function-entry instructions when a minor build changes logging code."""
    matches = []
    for start, end in pe.fn_ranges:
        raw = pe.rva_to_raw(start)
        if raw < 0 or pe.data[raw:raw + 4] != b"\x41\x57\x41\x56":
            continue
        insns = pe.disasm(pe.va_from_rva(start), min(end - start, 0x300))
        text = [(insn.mnemonic, insn.op_str) for insn in insns[:40]]
        score = 0
        score += 2 if any(m == "sub" and re.fullmatch(r"rsp, 0x[0-9a-f]+", o) for m, o in text[:6]) else 0
        score += 2 if any(m == "mov" and o == "ebx, edx" for m, o in text[:8]) else 0
        score += 2 if any(m == "mov" and o == "rsi, rcx" for m, o in text[:10]) else 0
        score += 2 if any(m == "cmp" and re.fullmatch(r"dword ptr \[rip \+ 0x[0-9a-f]+\], 2", o) for m, o in text[:14]) else 0
        score += 2 if any(m == "cmp" and re.fullmatch(r"qword ptr \[rip \+ 0x[0-9a-f]+\], 0", o) for m, o in text[:16]) else 0
        score += 1 if any(m == "mov" and o == "r9d, 2" for m, o in text[:26]) else 0
        score += 1 if any(m == "mov" and o == "r8d, 1" for m, o in text[:34]) else 0
        score += 2 if any(m == "mov" and o == "rdi, rax" for m, o in text[:38]) else 0
        score += 2 if any(
            m == "mov" and re.fullmatch(r"rax, qword ptr \[rsi \+ 0x[0-9a-f]+\]", o)
            for m, o in text[:90]
        ) else 0
        if score >= 10:
            matches.append((score, start))
    return sorted(matches, key=lambda item: (-item[0], item[1]))


def find_anchor_rvas(pe: PEImage) -> dict:
    """在文件字节中寻找全部锚点字符串的所有出现位置, 返回 name -> [rva...]"""
    found = {}
    for name, needle in ANCHOR_STRINGS.items():
        rvas = []
        start = 0
        while True:
            off = pe.data.find(needle, start)
            if off < 0:
                break
            rva = pe.raw_to_rva_off(off)
            if rva >= 0:
                rvas.append(rva)
            start = off + 1
        found[name] = rvas
    return found


def scan_xrefs(pe: PEImage, anchors: dict) -> dict:
    """扫描 .text 中所有 rip-relative LEA/MOV, 把指向锚点字符串的 xref 按 name 聚合"""
    text_raw = pe.data[pe.text_raw_start:pe.text_raw_end]
    anchor_va_map = {}
    for name, rvas in anchors.items():
        for rva in rvas:
            anchor_va_map[pe.base + rva] = name

    hits = {}
    for m in XREF_RE.finditer(text_raw):
        raw_off = pe.text_raw_start + m.start()
        op = text_raw[m.start() + 1]
        modrm = text_raw[m.start() + 2]
        disp = struct.unpack_from("<i", text_raw, m.start() + 3)[0]
        insn_va = pe.base + pe.raw_to_rva_off(raw_off)
        target = insn_va + 7 + disp
        name = anchor_va_map.get(target)
        if name is None:
            continue
        hits.setdefault(name, []).append(
            Xref(
                raw_off=raw_off,
                va=insn_va,
                rva=insn_va - pe.base,
                insn_hex=text_raw[m.start():m.start() + 24].hex(),
            )
        )
    return hits


def first_call_target(pe: PEImage, fn_start_rva: int, max_bytes: int = 0x1000) -> int | None:
    """反汇编函数入口, 返回从入口起第一个直接 call (e8) 的目标 rva"""
    insns = pe.disasm(pe.va_from_rva(fn_start_rva), max_bytes)
    for insn in insns:
        if insn.mnemonic == "call" and insn.op_str.startswith("0x"):
            target = int(insn.op_str, 16)
            rva = target - pe.base
            if pe.in_text(rva):
                return rva
    return None


def arg1_reg(insns, idx: int) -> str:
    """返回直到 idx 位置处首个参数的承载寄存器 (rcx, 若开头有 mov rxx,rcx 则取 rxx)"""
    for i in range(0, min(idx + 1, 6)):
        insn = insns[i]
        if insn.mnemonic == "mov" and insn.op_str.endswith(", rcx") and len(insn.op_str.split(",")) == 2:
            dst = insn.op_str.split(",")[0].strip()
            if dst.startswith("r"):
                return dst
    return "rcx"


def parse_mem(insn) -> tuple | None:
    """解析 'mov reg, [reg+imm]' / 'mov dword ptr [reg+imm], imm' / 'cmp dword ptr [reg+imm], imm'"""
    text = insn.op_str
    m = re.match(r"^(?:mov|lea|cmp)\s+(.+?),\s+(?:dword ptr |qword ptr |word ptr |byte ptr )?\[(\w+)\s*([+\-]\s*0x[0-9a-fA-F]+|\+?\d*)\]$", text)
    if m:
        left, reg, disp = m.group(1), m.group(2), m.group(3)
        try:
            imm = int(disp, 0) if disp.strip().startswith(("+", "-")) or "0x" in disp else int(disp)
        except ValueError:
            return None
        return (left.strip(), reg, imm)
    m2 = re.match(r"^(?:mov|cmp)\s+(?:dword ptr |qword ptr )?\[(\w+)\s*([+\-]?\s*0x[0-9a-fA-F]+)\],\s*(.+)$", text)
    if m2:
        reg, disp, right = m2.group(1), m2.group(2), m2.group(3)
        try:
            imm = int(disp, 0)
        except ValueError:
            return None
        return (right.strip(), reg, imm)
    return None


def find_onload_scene(pe: PEImage, fn_start_rva: int, first_arg: str) -> tuple | None:
    """
    在 OnLoadStart 函数内部寻找场景函数调用调用模式:
        mov rA, [first_arg + o0]; mov rB, [rA + o1]; ... call S
    返回 (o0, o1, S_rva) 或 None
    """
    insns = pe.disasm(pe.va_from_rva(fn_start_rva), 0x2000)
    last_two = []
    for i, insn in enumerate(insns):
        if insn.mnemonic != "call":
            last_two.append(insn)
            del last_two[:-24]
            continue
        if not insn.op_str.startswith("0x"):
            continue
        s_rva = int(insn.op_str, 16) - pe.base
        win = last_two[-24:]
        for j in range(len(win) - 2):
            d0 = parse_mem(win[j])
            if not d0:
                continue
            left0, reg0, off0 = d0
            if left0 != "r" and not left0.startswith("r"):
                continue
            for k in range(j, min(len(win), j + 16)):
                d1 = parse_mem(win[k])
                if not d1:
                    continue
                left1, reg1, off1 = d1
                if left1.startswith("r") and reg1 == left0 and 1050 <= off1 <= 1700:
                    if 24 <= off0 <= 160:
                        return (off0, off1, s_rva)
        last_two.append(insn)
        del last_two[:-24]
    return None


def find_scene_offsets(pe: PEImage, scene_rva: int) -> tuple | None:
    """
    场景函数内部寻找:
        mov rax, [arg1 + 8]; mov rax, [rax + o3]; mov rax, [rax + 16];
        mov/cmp eax, [rax + o5], 0x44D (1101)
    返回 (o2, o3, o4, o5) 或 None
    """
    insns = pe.disasm(pe.va_from_rva(scene_rva), 0x4000)
    a1 = arg1_reg(insns, len(insns))
    deep = None
    for i, insn in enumerate(insns):
        if insn.mnemonic != "mov":
            continue
        m = re.match(r"^(\w+),\s*\[(\w+)\s*\+\s*0x1?([0-9a-fA-F]+)\]$", insn.op_str)
        if not m:
            continue
        dst, src, heximm = m.groups()
        try:
            off = int(heximm, 16)
        except ValueError:
            continue
        if off == 8:
            if src != a1:
                continue
            deep = [dst, i, 8, None, None]
            continue
        if deep is None:
            continue
        if src == deep[0]:
            deep[2 + 1 + 1 - 1] = off if 900 <= off <= 1700 else deep[2]
            deep[3] = (dst, off)
            continue
        if deep[3] and src == deep[3][0]:
            deep[4] = (dst, off)
            continue
    return None


def find_scene_offsets2(pe: PEImage, scene_rva: int) -> tuple | None:
    """
    场景函数内部寻找 (与 find_scene_offsets 等价但更宽松):
        mov rX, [arg1 + 8]
        mov rY, [rX + o3]        o3 in 1000..1750
        mov rZ, [rY + 0x10]
        (cmp dword ptr [rZ + o5], 0x44D) 或 (mov eax,[rZ+o5]; cmp eax,0x44D)
    返回 (8, o3, 16, o5) 或 None
    """
    insns = pe.disasm(pe.va_from_rva(scene_rva), 0x8000)
    if not insns:
        return None
    argreg = arg1_reg(insns, len(insns))

    def load_off(insn, want_off, base_reg=None) -> tuple | None:
        if insn.mnemonic != "mov":
            return None
        m = re.match(r"^(\w+),\s*\[(\w+)\s*\+\s*0x([0-9a-fA-F]+)\]$", insn.op_str)
        if not m:
            return None
        dst, src, hx = m.groups()
        try:
            off = int(hx, 16)
        except ValueError:
            return None
        if want_off is not None and off != want_off:
            return None
        if base_reg is not None and src != base_reg:
            return None
        return (dst, off)

    for i, insn in enumerate(insns):
        step1 = load_off(insn, 8, argreg)
        if step1 is None:
            continue
        r1 = step1[0]
        for j in range(i + 1, min(i + 40, len(insns))):
            step2 = load_off(insns[j], None, r1)
            if step2 is None:
                continue
            r2, o3 = step2
            if not (900 <= o3 <= 1750):
                continue
            for k in range(j + 1, min(j + 40, len(insns))):
                step3 = load_off(insns[k], 0x10, r2)
                if step3 is None:
                    continue
                r3 = step3[0]
                tail = insns[k + 1:k + 60]
                for t in tail:
                    m = re.match(r"^dword ptr \[(\w+)\s*\+\s*0x([0-9a-fA-F]+)\],\s*0x44d$", t.op_str)
                    if t.mnemonic in ("cmp", "mov") and m and m.group(1) == r3:
                        o5 = int(m.group(2), 16)
                        if 400 <= o5 <= 620:
                            return (8, o3, 16, o5)
                    if t.mnemonic == "mov":
                        mm = re.match(r"^(\w+),\s*\[(\w+)\s*\+\s*0x([0-9a-fA-F]+)\]$", t.op_str)
                        if mm and mm.group(2) == r3:
                            for t2 in next(it for it in [[]] if False) or tail:
                                pass
    return None


def historical_scene_offsets(config_dir: Path, version: int) -> tuple[list[int] | None, int | None]:
    """Reuse the nearest local structure layout; layouts change much less often than RVAs."""
    candidates = []
    for path in config_dir.glob("addresses.*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            config_version = int(data["Version"])
            offsets = data.get("SceneOffsets")
            if isinstance(offsets, list) and len(offsets) == 6 and all(isinstance(x, int) for x in offsets):
                candidates.append((abs(config_version - version), config_version, offsets))
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
    if not candidates:
        return None, None
    _, source_version, offsets = min(candidates, key=lambda item: (item[0], -item[1]))
    return offsets, source_version


def extract_from_version(pe_path: Path, version: int, config_dir: Path = DEFAULT_CONFIG_DIR) -> dict | None:
    pe = PEImage.open(pe_path)
    log(f"PE: {pe_path.name}  ImageBase=0x{pe.base:X}  .text[0x{pe.text_va_start:X}-0x{pe.text_va_end:X}]")
    if pe.fn_starts:
        log(f".pdata 函数数: {len(pe.fn_starts)}")

    anchors = find_anchor_rvas(pe)
    log("锚点字符串: " + ", ".join(f"{k}={len(v)}处" for k, v in anchors.items()))

    if "SendToClientFilter" not in anchors or not anchors["SendToClientFilter"]:
        log("缺少 SendToClientFilter 锚点, 无法定位 CDPFilterHookOffset")
        return None
    str_rva = anchors["SendToClientFilter"][0]
    log(f"SendToClientFilter 字符串 rva=0x{str_rva:X}")

    xrefs = scan_xrefs(pe, anchors)

    cdp_xrefs = xrefs.get("SendToClientFilter", [])
    log(f"SendToClientFilter xref 数: {len(cdp_xrefs)}")
    for x in cdp_xrefs:
        log(f"  xref rva=0x{x.rva:X} va=0x{x.va:X} {x.insn_hex[:40]}")
    if not cdp_xrefs:
        return None

    fn = pe.function_by_rva(cdp_xrefs[0].rva)
    if fn is None:
        log("xref 不在任何 .pdata 函数范围内, 需要人工确认")
        return None
    fn_start = fn[0]
    log(f"xref 所在函数 rva=0x{fn_start:X}")
    cdp_rva = None
    if fn_start == cdp_xrefs[0].rva:
        cdp_rva = first_call_target(pe, fn_start)
    else:
        cdp_rva = first_call_target(pe, fn_start)
    if cdp_rva is None:
        log("无法确定 xref 函数内第一个 call 目标")
        return None
    log(f"CDPFilterHookOffset = 0x{cdp_rva:X}")

    signature_matches = find_loadstart_by_signature(pe)
    if signature_matches:
        log("OnLoadStart 指令指纹: " + ", ".join(f"0x{x:X}" for x in signature_matches))
    else:
        relaxed = find_loadstart_by_relaxed_signature(pe)
        log("OnLoadStart 宽松指纹候选: " + ", ".join(f"0x{x:X}(score={score})" for score, x in relaxed[:8]) if relaxed else "OnLoadStart 指令指纹: 无")
        if relaxed and (len(relaxed) == 1 or relaxed[0][0] > relaxed[1][0] + 2):
            signature_matches = [relaxed[0][1]]

    if len(signature_matches) == 1:
        load_rva = signature_matches[0]
    else:
        onload_xrefs = xrefs.get("OnLoadStart", [])
        cands = []
        for x in onload_xrefs:
            fn = pe.function_by_rva(x.rva)
            if fn:
                cands.append((x, fn[0]))
        if not cands:
            log("未找到 OnLoadStart 唯一指纹或 xref")
            return None

        best = None
        log(f"OnLoadStart xref 候选函数: {len(cands)}")
        scored = []
        for x, f0 in cands:
            score = 0
            app_x = xrefs.get("applet_index_container.cc", [])
            if any(pe.function_by_rva(a.rva) and pe.function_by_rva(a.rva)[0] == f0 for a in app_x):
                score += 100
            for name in ("AppletIndexContainer::OnLoadStart", "[perf] AppletIndexContainer::OnLoadStart"):
                if any(pe.function_by_rva(a.rva) and pe.function_by_rva(a.rva)[0] == f0 for a in xrefs.get(name, [])):
                    score += 100
            scored.append((score, x, f0))
            log(f"  候选: fn rva=0x{f0:X} (xref=0x{x.rva:X}) 评分={score}")
        scored.sort(key=lambda item: (-item[0], item[2]))
        if scored and (scored[0][0] > 0 or len(scored) == 1):
            best = scored[0]
        if best is None:
            log("无法消歧 OnLoadStart, 拒绝生成可能错误的配置")
            return None
        load_rva = best[2]

    log(f"LoadStartHookOffset = 0x{load_rva:X}")

    scene_offsets, source_version = historical_scene_offsets(config_dir, version)
    if scene_offsets is None:
        log("本地没有可继承的 6 元 SceneOffsets, 拒绝生成配置")
        return None
    log(f"SceneOffsets = {scene_offsets} (沿用本地 addresses.{source_version}.json 的结构布局)")
    return {
        "Version": version,
        "LoadStartHookOffset": f"0x{load_rva:X}",
        "CDPFilterHookOffset": f"0x{cdp_rva:X}",
        "SceneOffsets": scene_offsets,
    }


def list_installed_versions() -> list:
    versions = []
    for wmpf_dir in BASE_WMPF_DIRS:
        if not wmpf_dir.exists():
            continue
        for child in wmpf_dir.iterdir():
            if not child.is_dir():
                continue
            try:
                v = int(child.name)
            except ValueError:
                continue
            rt = child / "extracted" / "runtime"
            if rt.exists():
                versions.append((v, rt))
    versions.sort(key=lambda t: -t[0])
    return versions


def nearest_config(config_dir: Path, version: int) -> tuple | None:
    best = None
    for f in config_dir.glob("addresses.*.json"):
        try:
            v = int(f.stem.split(".")[1])
        except (IndexError, ValueError):
            continue
        if best is None or abs(v - version) < abs(best[0] - version):
            best = (v, f)
    return best


def write_config(config_dir: Path, data: dict, force: bool) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    target = config_dir / f"addresses.{data['Version']}.json"
    if target.exists() and not force:
        try:
            current = json.loads(target.read_text(encoding="utf-8"))
            if current == data:
                log(f"{target.name} 已是最新, 无需写入")
                return target
        except (OSError, json.JSONDecodeError):
            pass
        sys.exit(f"{target.name} 已存在且内容不同, 如需覆盖请加 --force")
    fd, tmp = tempfile.mkstemp(dir=str(config_dir), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
            f.write("\n")
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    log(f"已写入 {target}")
    return target


def main() -> None:
    ap = argparse.ArgumentParser(description="WMPFDebugger 地址自动更新器")
    ap.add_argument("--version", type=int, help="指定 WMPF 版本号")
    ap.add_argument("--pe", type=Path, help="直接指定 PE 文件路径")
    ap.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    ap.add_argument("--write", action="store_true", help="写入 config")
    ap.add_argument("--force", action="store_true", help="允许覆盖已有 config")
    ap.add_argument("--list", action="store_true", help="列出已安装版本的 WMPF")
    args = ap.parse_args()

    if args.list:
        for v, rt in list_installed_versions():
            print(f"{v}: {rt}")
        return

    if args.pe is not None:
        pe_path = args.pe
        version = args.version or 0
    else:
        versions = list_installed_versions()
        if not versions:
            sys.exit("未在本机发现 RadiumWMPF 安装目录")
        if args.version is not None:
            vv = next((v for v, rt in versions if v == args.version), None)
            if vv is None:
                sys.exit(f"本机未安装 WMPF {args.version}")
            version, rt = vv, dict((v, rt) for v, rt in versions)[vv]
        else:
            version, rt = versions[0]
        candidates = []
        if version >= OLD_VERSION_BOUNDARY:
            candidates.append(rt / "flue.dll")
        candidates.append(rt / "WeChatAppEx.exe")
        pe_path = next((p for p in candidates if p.exists()), None)
        if pe_path is None:
            sys.exit(f"WMPF {version} 运行时目录中未找到 flue.dll / WeChatAppEx.exe: {rt}")

    log(f"分析: {pe_path} (WMPF {version})")
    data = extract_from_version(pe_path, version)
    if data is None:
        sys.exit("提取失败, 请人工对照 ADAPTATION.md 处理")

    print()
    print(json.dumps(data, ensure_ascii=False, indent=4))

    nb = nearest_config(args.config_dir, version)
    if nb:
        import json as _json
        try:
            old = _json.loads(nb[1].read_text(encoding="utf-8"))
            print()
            print(f"参考: addresses.{nb[0]}.json")
            print(f"  LoadStartHookOffset: {old['LoadStartHookOffset']} -> {data['LoadStartHookOffset']}")
            print(f"  CDPFilterHookOffset: {old['CDPFilterHookOffset']} -> {data['CDPFilterHookOffset']}")
            print(f"  SceneOffsets: {old['SceneOffsets']} -> {data.get('SceneOffsets')}")
        except Exception as e:
            print(f"(无最近参照: {e})")

    if args.write:
        write_config(args.config_dir, data, args.force)


if __name__ == "__main__":
    main()
