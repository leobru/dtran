#!/usr/bin/env python3
"""besm6dec - a BESM-6 Pascal (Pascal-Monitor / Pascal-Autocode) decompiler.

Second stage of the pipeline: `dtran -d` emits a bespoke pseudo-assembly
listing; this tool lifts it into Pascal-like pseudocode.  It replaces the four
Perl forks decomp1..4.pl with one engine whose family/target differences are
expressed as *data* (a Dialect profile) rather than as forked code.

Architecture (token pipeline):

    nodes = tokenize(text)                # list[Node]
    for p in profile.pipeline:            # each pass: list[Node] -> list[Node]
        nodes = p(nodes)
    sys.stdout.write(render(nodes))

A Node is one of:
  * Insn    - a decoded instruction or data directive (reg, op, arg, label)
  * Comment - a banner/`C ...` line, carried through verbatim
  * Text    - emitted pseudocode (produced by later passes; rendered as-is)

This module currently implements the spine (tokenize/render/pipeline/CLI) and
is grown pass-by-pass; see tasks 3-5.  The Perl variants in ../decomp*.pl are
the bring-up reference (see check.sh); output is not required to byte-match
them, only to be a faithful, reviewed superset.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field, replace
from typing import Callable, Optional


# --------------------------------------------------------------------------- #
# Node model
# --------------------------------------------------------------------------- #

@dataclass
class Insn:
    """A decoded instruction or data directive: `[label:][reg],op,arg`.

    `reg` is the index-register prefix ('' when absent).  `op` is the mnemonic
    (may contain +-*, e.g. A-X, J+M, *50, or a directive INT/GOST/BSS/NAME).
    `arg` is the operand text, kept verbatim (it can carry ` . octal` comments,
    `|gost|` payloads, `(literal)`, `P/x`, and trailing spaces).
    `indent` preserves the source leading whitespace for lossless round-trip.
    """
    op: str
    arg: str = ""
    reg: str = ""
    label: Optional[str] = None
    indent: str = " "

    def render(self) -> str:
        lab = f"{self.label}:" if self.label is not None else ""
        return f"{self.indent}{lab}{self.reg},{self.op},{self.arg}"


@dataclass
class Comment:
    """A line carried through verbatim (banner, `C ...` info comments)."""
    text: str

    def render(self) -> str:
        return self.text


@dataclass
class Text:
    """Emitted pseudocode produced by later passes; rendered as-is.

    `raw` text is emitted verbatim by `out_line` (no leading-space indent, no
    trailing `;`), so a pass can lay down an exact multi-line block such as a
    `_proced .../\\n_var ...:integer; _(` declaration.
    """
    text: str
    indent: str = " "
    raw: bool = False

    def render(self) -> str:
        return f"{self.indent}{self.text}"


@dataclass
class Label:
    """A code (`Lnnnn`) or data (`/nnnn`) label on its own line.

    The Perl splits every `label:` off its instruction by inserting `,BSS,`
    so that pattern matches can treat the label as a unit; we make it a
    first-class node instead.  Renders in that same `name:,BSS,` surface form
    to keep the bring-up diffs against the Perl small.
    """
    name: str

    def render(self) -> str:
        return f" {self.name}:,BSS,;"


@dataclass
class Header:
    """A recognized subroutine header, e.g.
    `L2012: Level 2 procedure with 0 arguments and 1 (or a func with 0) locals`.

    `desc` is the full human description (parsed back by processprocs for level
    tracking); `role` is '', ' (body)' or ' (header)' once hoisting runs.
    """
    off: str
    desc: str
    label_prefix: str = "L"
    role: str = ""

    def out(self) -> str:
        return f" {self.label_prefix}{self.off}: {self.desc}{self.role};"

    def render(self) -> str:
        return self.out()


@dataclass
class Raw:
    """An indented source line that did not fit the instruction grammar."""
    text: str

    def render(self) -> str:
        return self.text


Node = object  # Insn | Comment | Text | Label | Raw


# --------------------------------------------------------------------------- #
# Node helpers for sequence-matching passes
# --------------------------------------------------------------------------- #

def is_insn(n: Node, op: Optional[str] = None, reg: Optional[str] = None) -> bool:
    return (isinstance(n, Insn)
            and (op is None or n.op == op)
            and (reg is None or n.reg == reg))


# --------------------------------------------------------------------------- #
# Tokenizer
# --------------------------------------------------------------------------- #

# After optional `label:` is peeled, an instruction body is `[reg],op,arg`.
_BODY = re.compile(r"^(\d*),([^,]*),(.*)$", re.DOTALL)


def _split_label(body: str) -> tuple[Optional[str], str]:
    """Peel a leading `label:` when the colon precedes the first comma.

    Returns (label, rest).  Label text is kept verbatim (it may include a
    trailing space, as in `KALAH :`).  `/204:` -> ('/204', ',INT,...').
    """
    colon = body.find(":")
    comma = body.find(",")
    if colon != -1 and (comma == -1 or colon < comma):
        return body[:colon], body[colon + 1:]
    return None, body


def tokenize(text: str) -> list[Node]:
    """Parse a raw `dtran -d` listing into a list of Nodes.

    Each physical line is one node.  Lines indented with whitespace are
    instructions/data; flush-left lines (`C ...` banners) are comments.
    """
    nodes: list[Node] = []
    for line in text.split("\n"):
        stripped = line.lstrip(" ")
        indent = line[: len(line) - len(stripped)]
        if indent == "":
            # flush-left: a banner / info comment (or a blank line)
            nodes.append(Comment(line))
            continue
        label, rest = _split_label(stripped)
        m = _BODY.match(rest)
        if not m:
            nodes.append(Raw(line))
            continue
        reg, op, arg = m.group(1), m.group(2), m.group(3)
        nodes.append(Insn(op=op, arg=arg, reg=reg, label=label, indent=indent))
    return nodes


def render(nodes: list[Node]) -> str:
    """Lossless render used by --roundtrip (reproduces the raw listing)."""
    return "\n".join(n.render() for n in nodes)


def out_line(n: Node) -> str:
    """Decompiler-output render of one node: the Perl's `;`-terminated,
    space-indented surface form (flush-left `C ...` comments excepted)."""
    if isinstance(n, Insn):
        lab = f"{n.label}:" if n.label is not None else ""
        return f" {lab}{n.reg},{n.op},{n.arg};"
    if isinstance(n, Text):
        return n.text if n.raw else f" {n.text};"
    if isinstance(n, Label):
        return f" {n.name}:,BSS,;"
    if isinstance(n, Header):
        return n.out()
    if isinstance(n, Comment):
        return n.text
    return n.render()  # Raw


def render_out(nodes: list[Node]) -> str:
    return "\n".join(out_line(n) for n in nodes)


# --------------------------------------------------------------------------- #
# Passes - front-end normalization (slice 1)
#
# Each pass is `list[Node] -> list[Node]`.  Per-instruction edits mutate in
# place and return the list; structural passes build a fresh list.  The Dialect
# `d` carries the family deltas (registers, name prefixes, toggles).
# --------------------------------------------------------------------------- #

# A 3-character operand-bearing op with no index-register semantics (no 'M').
# Matches the Perl `[^M][^M][^M]`: ATX, XTA, A+X, *70 ... but not VTM/UTM/MTJ.
def _is_dataop(op: str) -> bool:
    return len(op) == 3 and "M" not in op


def normalize_offsets(d: Dialect, nodes: list[Node]) -> list[Node]:
    """`,;` -> `,0;`: an instruction with an empty operand gets offset 0."""
    for n in nodes:
        if isinstance(n, Insn) and n.arg == "":
            n.arg = "0"
    return nodes


def for_loop_transform(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Rotate `UJ L ; body ; L:pre,ATX,X` into a stack-friendly shape so the
    loop variable's init and increment become explicit assignments:
        pre,ATX,X ; UJ L ; body ; pre,ATX,X ; L:pre,XTA,X
    Runs before labels are split (the label is still on the ATX). decomp1/4
    only -- decomp2/3 recognize loops separately, at the end of the pipeline.
    """
    if d.track_regs:
        return nodes
    while True:
        # index each label to where it is defined (labels are unique); O(n)/pass
        label_idx: dict[str, int] = {}
        for k, nd in enumerate(nodes):
            if isinstance(nd, Insn) and nd.label is not None:
                label_idx.setdefault(nd.label, k)
        done = False
        for i, n in enumerate(nodes):
            if not (is_insn(n, "UJ", "") and n.arg):
                continue
            j = label_idx.get(n.arg)
            if j is not None and j > i and is_insn(nodes[j], "ATX"):
                m = nodes[j]
                body = nodes[i + 1:j]
                atlabel = Insn(op="XTA", reg=m.reg, arg=m.arg, label=n.arg)
                nodes = (nodes[:i]
                         + [Insn(op="ATX", reg=m.reg, arg=m.arg), n] + body
                         + [Insn(op="ATX", reg=m.reg, arg=m.arg), atlabel]
                         + nodes[j + 1:])
                done = True
                break
        if not done:
            break
    return nodes


def normalize_utm_wrap(d: Dialect, nodes: list[Node]) -> list[Node]:
    """decomp4: `,UTM,327xx` -> signed `,UTM,-(32768-xx)` (16-bit wrap)."""
    for n in nodes:
        if isinstance(n, Insn) and n.op == "UTM" and re.fullmatch(r"327\d\d", n.arg):
            n.arg = str(int(n.arg) - 32768)
    return nodes


def normalize_global_refs(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Canonicalize the UTC/VTM addressing idioms for globals.

    Ports the Perl two-line rewrites that move a `UTC` displacement onto the
    following `VTM`/data op so a later pass sees a plain `1,op,disp`:
      `1,UTC,0 ; r,VTM,disp`     -> `1,UTC,disp ; r,VTM,0`
      `,UTC,disp ; 1,op,0`       -> `1,op,disp`
      `,UTC,disp ; r,VTM,0`      -> `r,VTM,disp`        (Pascal-Autocode)
    """
    out: list[Node] = []
    i = 0
    while i < len(nodes):
        a, b = nodes[i], (nodes[i + 1] if i + 1 < len(nodes) else None)
        if (is_insn(a, "UTC", "1") and a.arg == "0"
                and is_insn(b, "VTM") and b.reg):
            out.append(replace(a, arg=b.arg))
            out.append(replace(b, arg="0"))
            i += 2
            continue
        if is_insn(a, "UTC") and a.reg == "" and is_insn(b, reg="1") and b.arg == "0":
            out.append(replace(b, reg="1", arg=a.arg))
            i += 2
            continue
        if (d.runtime_reg == "12"  # Pascal-Autocode (decomp2/3)
                and is_insn(a, "UTC") and a.reg == ""
                and is_insn(b, "VTM") and b.reg and b.arg == "0"):
            out.append(replace(b, arg=a.arg))
            i += 2
            continue
        out.append(a)
        i += 1
    return out


def drop_empty_labels(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Pascal-Autocode: drop word-alignment markers -- a `:` with no name, which
    would otherwise render as a useless ` :,BSS,;` line (decomp3 l.15).  Kept for
    DMS, where ` :,BSS,` is part of the frameless prologue."""
    if not d.track_regs:
        return nodes
    for n in nodes:
        if isinstance(n, Insn) and n.label == "":
            n.label = None
    return nodes


def drop_frame_restore(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Pascal-Autocode: drop the `11,MTJ,d` frame-restoration left after a
    scope-crossing call; it otherwise splits the epilogue and blocks function
    return recognition (decomp3 l.62)."""
    if not d.track_regs:
        return nodes
    return [n for n in nodes
            if not (is_insn(n, "MTJ", "11") and re.fullmatch(r"\d", n.arg))]


def split_labels(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Peel every `label:` onto its own Label node (Perl's `,BSS,` insertion).

    The very first node (the `NAME` banner, e.g. `PASCODER:,NAME,...`) is left
    intact: the Perl regex only splits a label preceded by `;`, which the
    leading unit is not.
    """
    out: list[Node] = []
    for idx, n in enumerate(nodes):
        if idx > 0 and isinstance(n, Insn) and n.label is not None:
            out.append(Label(n.label))
            out.append(replace(n, label=None))
        else:
            out.append(n)
    return out


def global_via_reg1(d: Dialect, nodes: list[Node]) -> list[Node]:
    """`1,<dataop>,N` -> `,<dataop>,<prefix>Nz`: a global addressed via index
    register 1 becomes a named global reference (prefix is dialect-specific)."""
    for n in nodes:
        if isinstance(n, Insn) and n.reg == "1" and _is_dataop(n.op) and n.arg.isdigit():
            n.reg = ""
            n.arg = f"{d.global_prefix}{n.arg}z"
    return nodes


def normalize_utc_vtm(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Pascal-Autocode: after globals are named, fold a UTC address-modifier
    into the following register load: `,UTC,X ; r,VTM,0` -> `r,VTM,X`
    (decomp2 l.37; lets `1,UTC,134 ; 13,VTM,0` become `R13 := &gl134z`)."""
    if not d.track_regs:
        return nodes
    out: list[Node] = []
    i = 0
    while i < len(nodes):
        a = nodes[i]
        b = nodes[i + 1] if i + 1 < len(nodes) else None
        if (is_insn(a, "UTC") and a.reg == "" and is_insn(b, "VTM")
                and b.reg and b.arg == "0"):
            out.append(replace(b, arg=a.arg))
            i += 2
            continue
        out.append(a)
        i += 1
    return out


_P1D = re.compile(r"P/1D *\+(\d+)")


def global_via_p1d(d: Dialect, nodes: list[Node]) -> list[Node]:
    """decomp1 (DMS): data-block references `P/1D   +N` -> globNz, so the
    static-init section `,XTA,v ; ,ATX,P/1D +N` becomes `globNz := v`."""
    if d.label_style != "*":          # DMS only (decomp4 keeps this disabled)
        return nodes
    for n in nodes:
        if isinstance(n, Insn):
            n.arg = _P1D.sub(lambda m: f"{d.global_prefix}{m.group(1)}z", n.arg)
    return nodes


def vtm_uj_shortcut(d: Dialect, nodes: list[Node]) -> list[Node]:
    """`13,VTM,x ; ,UJ,y` -> `13,VJM,y ; ,UJ,x`: a call expressed as a return-
    address load plus jump becomes a direct call + tail jump."""
    out: list[Node] = []
    i = 0
    while i < len(nodes):
        a, b = nodes[i], (nodes[i + 1] if i + 1 < len(nodes) else None)
        if is_insn(a, "VTM", "13") and is_insn(b, "UJ", ""):
            out.append(replace(a, op="VJM", arg=b.arg))
            out.append(replace(b, arg=a.arg))
            i += 2
            continue
        out.append(a)
        i += 1
    return out


# --------------------------------------------------------------------------- #
# Passes - prologue recognition + processprocs (slice 2)
# --------------------------------------------------------------------------- #

_PL = re.compile(r"P/(\d)\s*$")


def _rtype(d: Dialect, off: str) -> Optional[str]:
    """Routine kind ('p'/'f') from the target's routines table, if known.

    Absent (the common case for these samples) yields None -> the ambiguous
    'procedure ... (or a func ...)' wording, matching the Perl run without
    routines.txt."""
    return d.const_map.get(f"rtype:{off}")  # placeholder until profiles land


def _noargs_desc(d, off, l, n):
    if n == 1:
        return f"Level {l} procedure with 0 arguments and 0 locals"
    rt = _rtype(d, off)
    if rt == "f":
        return f"Level {l} function with 0 arguments and {n - 2} locals"
    if rt == "p":
        return f"Level {l} procedure with 0 arguments and {n - 1} locals"
    return (f"Level {l} procedure with 0 arguments and {n - 1} "
            f"(or a func with {n - 2}) locals")


def _manyargs_desc(d, off, l, n, m):
    rt = _rtype(d, off)
    if rt == "f":
        return f"Level {l} function with {n - 4} arguments and {m - n + 2} locals"
    if rt == "p":
        return f"Level {l} procedure with {n - 3} arguments and {m - n + 2} locals"
    return (f"Level {l} procedure with {n - 3} (or a func with {n - 4}) "
            f"arguments and {m - n + 2} locals")


def its11_prologue(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Pascal-Autocode: rewrite the ITS/NTR/MTJ register-save prologue into the
    standard many-args form so recognize_prologues can match it (decomp3 l.88):

      ,ITS,11 ; 15,UTM,-N1 ; ,NTR,3 ; 15,MTJ,11 ; 15,MTJ,L ; ,ITS,14 ;
        ,ITS,c ; 11,ATX,2 ; 15,UTM,N3
      -> 15,ATX,0 ; 15,UTM,-N1 ; 12,VJM,P/L ; 15,UTM,N3

    Runs after split_labels so the routine's `L...:` label (which precedes the
    sequence) is preserved on its own node.
    """
    if not d.track_regs:
        return nodes
    out: list[Node] = []
    i = 0
    while i < len(nodes):
        w = nodes[i:i + 9]
        if (len(w) == 9
                and is_insn(w[0], "ITS", "") and w[0].arg == "11"
                and is_insn(w[1], "UTM", "15") and re.fullmatch(r"-\d+", w[1].arg)
                and is_insn(w[2], "NTR", "") and w[2].arg == "3"
                and is_insn(w[3], "MTJ", "15") and w[3].arg == "11"
                and is_insn(w[4], "MTJ", "15") and re.fullmatch(r"\d", w[4].arg)
                and is_insn(w[5], "ITS", "") and w[5].arg == "14"
                and is_insn(w[6], "ITS", "") and len(w[6].arg) == 1
                and is_insn(w[7], "ATX", "11") and w[7].arg == "2"
                and is_insn(w[8], "UTM", "15") and re.fullmatch(r"\d+", w[8].arg)):
            out.append(Insn(reg="15", op="ATX", arg="0"))
            out.append(Insn(reg="15", op="UTM", arg=w[1].arg))
            out.append(Insn(reg="12", op="VJM", arg=f"P/{w[4].arg}"))
            out.append(Insn(reg="15", op="UTM", arg=w[8].arg))
            i += 9
            continue
        out.append(nodes[i])
        i += 1
    return out


def recognize_prologues(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Replace a Label + register-save prologue with a `==========` separator
    and a Header describing the routine (level, args, locals).

    Five shapes (runtime call via `d.runtime_reg`, =14 DMS/Pascal-Monitor, =12 Pascal-Autocode):
      noargs : VJM,P/l ; 15,UTM,n
      1 arg  : 15,ATX,3|4 ; VJM,P/l ; 15,UTM,n     (3=proc, 4=func)
      manyarg: 15,ATX,0 ; 15,UTM,-n ; VJM,P/l ; 15,UTM,m
      frame- : ,NTR,7 ; 13,MTJ,l                    (level = l-1)
    """
    R = d.runtime_reg
    pre = d.label_style if d.label_style == "*" else "L"
    out: list[Node] = []
    i = 0

    def ins(k):  # k-th node after the label, or None
        j = i + 1 + k
        return nodes[j] if j < len(nodes) else None

    def vjm_level(n):
        m = isinstance(n, Insn) and n.op == "VJM" and n.reg == R and _PL.match(n.arg)
        return int(m.group(1)) if m else None

    def utm(n):
        return int(n.arg) if is_insn(n, "UTM", "15") and re.fullmatch(r"-?\d+", n.arg) else None

    while i < len(nodes):
        lab = nodes[i]
        if isinstance(lab, Label) and lab.name.startswith(("L", "*")):
            off = lab.name[1:]
            a0, a1, a2, a3 = ins(0), ins(1), ins(2), ins(3)
            consumed = desc = None
            # manyargs (longest first)
            if (is_insn(a0, "ATX", "15") and a0.arg == "0"
                    and is_insn(a1, "UTM", "15") and a1.arg.startswith("-")
                    and (lv := vjm_level(a2)) is not None and (m := utm(a3)) is not None):
                desc = _manyargs_desc(d, off, lv, int(a1.arg[1:]), m); consumed = 4
            elif (is_insn(a0, "ATX", "15") and a0.arg in ("3", "4")
                  and (lv := vjm_level(a1)) is not None and (n := utm(a2)) is not None):
                if a0.arg == "3":
                    desc = f"Level {lv} procedure with 1 argument and {n - 2} locals"
                else:
                    desc = f"Level {lv} function with 1 argument and {n - 3} locals"
                consumed = 3
            elif (lv := vjm_level(a0)) is not None and (n := utm(a1)) is not None:
                desc = _noargs_desc(d, off, lv, n); consumed = 2
            elif is_insn(a0, "NTR", "") and a0.arg == "7" and is_insn(a1, "MTJ", "13"):
                desc = f"Level {int(a1.arg) - 1} procedure with no frame"; consumed = 2
            elif (is_insn(a0, "NTR", "") and a0.arg == "7" and isinstance(a1, Label)
                  and is_insn(a2, "MTJ", "13")):
                # DMS: a word-alignment ` :,BSS,` label sits between NTR,7 and MTJ
                desc = f"Level {int(a2.arg) - 1} procedure with no frame"; consumed = 3
            if desc is not None:
                out.append(Text("=========="))
                out.append(Header(off=off, desc=desc, label_prefix=pre))
                i += 1 + consumed
                continue
        out.append(lab)
        i += 1
    return out


_HDR = re.compile(r"Level (\d) (\w+) with (\d+) argument")
_HDR2 = re.compile(r"Level (\d) (\w+) with (\d+).* and (\d+)")


def processprocs(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Name local variables, hoist headers by nesting level, and resolve
    references to enclosing routines' variables.  Faithful port of the Perl
    `processprocs`, operating on Header/Insn nodes instead of a `;`-string.
    """
    an, vn = ("arg", "var") if d.label_style == "*" else ("a", "v")

    # --- part 1: name locals addressed via display registers 2..curlev ---
    curlev, funcname, args, unkn = -1, "", 0, False
    knownregs: set[str] = set()
    i = 0
    while i < len(nodes):
        n = nodes[i]
        if isinstance(n, Header):
            m = _HDR.search(n.desc)
            if m:
                curlev = int(m.group(1)); args = int(m.group(3))
                funcname = f"{n.label_prefix}{n.off}" if m.group(2) == "function" else ""
                unkn = " or " in n.desc
                knownregs = set()
            i += 1
            continue
        if isinstance(n, Insn):
            r = n.reg
            # local via display register 2..curlev (only inside a known proc)
            if (curlev != -1 and r.isdigit() and len(r) == 1 and "2" <= r <= str(curlev)
                    and _is_dataop(n.op) and n.arg.isdigit()):
                idx = int(n.arg) - 3
                if unkn or r != str(curlev):
                    name = f"l{r}loc{idx}z"
                    if funcname:
                        name = name.replace(f"l{curlev}loc0z", funcname)
                elif funcname:
                    name = f"l{r}{an}{idx}z" if idx <= args else f"l{r}{vn}{idx - args}z"
                    name = name.replace(f"l{curlev}{an}0z", funcname)
                else:
                    name = (f"l{r}{an}{idx + 1}z" if idx < args
                            else f"l{r}{vn}{idx - args + 1}z")
                n.reg, n.arg = "", name
                i += 1
                continue
            # DMS/Pascal-Monitor register faking (runs regardless of curlev, as
            # in the Perl `[$curlev-69]` class): r,VTM,x -> XTA &x / ATX Rr, then
            # later reads of Rr resolve.  decomp2/3 track registers in the stack
            # machine instead, so this whole block is off when track_regs is set.
            if not d.track_regs and _reg_work(r, curlev):
                if n.op == "VTM":
                    nodes[i:i + 1] = [Insn(op="XTA", arg=f"&{n.arg}"),
                                      Insn(op="ATX", arg=f"R{r}")]
                    knownregs.add(r)
                    i += 2
                    continue
                if r in knownregs and _is_dataop(n.op) and n.arg.isdigit():
                    n.reg, n.arg = "", f"R{r}->{n.arg}"; i += 1; continue
                if r in knownregs and n.op == "ITA":
                    nodes[i] = Insn(op="XTA", arg=f"R{r}"); i += 1; continue
            # return from a frameless procedure: (curlev+1),UJ,0 -> RETURN
            # (decompG renames the runtime return to EXIT up front, l.204).
            if curlev != -1 and n.reg == str(curlev + 1) and n.op == "UJ" and n.arg == "0":
                nodes[i] = Text("EXIT" if d.underscore_kw else "RETURN")
        i += 1

    # --- part 2: hoist procedure headers up by nesting level ---
    nodes = _hoist_headers(d, nodes)

    # --- part 3: resolve references to enclosing routines' locals ---
    procname: dict[int, str] = {}
    isfunc: dict[int, bool] = {}
    nargs: dict[int, int] = {}
    for n in nodes:
        if isinstance(n, Header):
            m = _HDR2.search(n.desc)
            if m:
                lev = int(m.group(1))
                procname[lev] = f"{n.label_prefix}{n.off}"
                isfunc[lev] = m.group(2) == "function"
                nargs[lev] = int(m.group(3))
        elif isinstance(n, Insn):
            n.arg = re.sub(
                r"l(\d)loc(\d+)z",
                lambda mm: _rename_locref(int(mm.group(1)), int(mm.group(2)),
                                          procname, isfunc, nargs, an, vn),
                n.arg)
    return nodes


def _reg_work(reg: str, curlev: int) -> bool:
    """Perl `[$curlev-69]`: a one-char register in curlev..'6', or '9'.
    Outside any routine (curlev == -1) the class is `[-1-69]`, i.e. 1..6, 9."""
    if not (reg.isdigit() and len(reg) == 1):
        return False
    if reg == "9":
        return True
    lo = "1" if curlev < 0 else str(curlev)
    return lo <= reg <= "6"


def _rename_locref(lev, idx, procname, isfunc, nargs, an, vn):
    na = nargs.get(lev, 0)
    if isfunc.get(lev):
        if idx == 0:
            return procname.get(lev, f"l{lev}loc0z")
        return f"l{lev}{an}{idx}z" if idx <= na else f"l{lev}{vn}{idx - na}z"
    return f"l{lev}{an}{idx + 1}z" if idx < na else f"l{lev}{vn}{idx - na + 1}z"


# --------------------------------------------------------------------------- #
# Passes - pre-stack-machine recognizers (slice 3)
# --------------------------------------------------------------------------- #

# Operand literals shared by all targets (decomp1/4 templates + NIL).
_TEMPLATES = {
    "(74000C)": "NIL",
    "(360100B)": "ASN64template",
    "(400016B)": "ATI14template",
    "(370007B)": "NTR7template",
}


def subst_constants(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Substitute pre-seeded global constants (gNz -> e1/multmask/...) and the
    universal operand templates (e.g. `(74000C)` -> NIL) in operands."""
    for n in nodes:
        if not isinstance(n, Insn):
            continue
        for k, v in _TEMPLATES.items():
            if k in n.arg:
                n.arg = n.arg.replace(k, v)
        for k, v in d.const_map.items():
            if k.startswith("rtype:"):
                continue
            if k in n.arg:
                n.arg = n.arg.replace(k, v)
    return nodes


# compare op + branch op  ->  (rewritten compare op, marker kind)
_FOLD = {
    ("AAX", "UZA"): ("AAX", "ifnot"), ("AEX", "UZA"): ("CEQ", "ifgoto"),
    ("A-X", "UZA"): ("CGE", "ifgoto"), ("X-A", "UZA"): ("CLE", "ifgoto"),
    ("AAX", "U1A"): ("AAX", "ifgoto"), ("AEX", "U1A"): ("CNE", "ifgoto"),
    ("A-X", "U1A"): ("CLT", "ifgoto"), ("X-A", "U1A"): ("CGT", "ifgoto"),
}


def fold_branches(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Fold a compare + conditional jump into a comparison op and an
    `ifgoto`/`ifnot <target>` marker the stack machine consumes.

    AEX/A-X/X-A become CEQ/CGE/CLE (UZA) or CNE/CLT/CGT (U1A); AAX stays AAX
    (a bit-test).  A bare `XTA value` before the jump gets an explicit `,CEQ,0`.
    """
    out: list[Node] = []
    i = 0
    while i < len(nodes):
        a, b = nodes[i], (nodes[i + 1] if i + 1 < len(nodes) else None)
        if is_insn(b, "UZA") or is_insn(b, "U1A"):
            br = b.op
            if isinstance(a, Insn) and (a.op, br) in _FOLD:
                newop, kind = _FOLD[(a.op, br)]
                out.append(replace(a, op=newop))
                out.append(Text(f"{kind} {b.arg}"))
                i += 2
                continue
            if isinstance(a, Insn) and a.op == "XTA":
                out.append(a)
                out.append(Insn(op="CEQ" if br == "UZA" else "CNE", arg="0"))
                out.append(Text(f"ifgoto {b.arg}"))
                i += 2
                continue
            # a Boolean already on the stack: set membership (`CALL P/IN`) or
            # eof/eoln -> the jump becomes ifnot (UZA) / ifgoto (U1A).
            if isinstance(a, Text) and (re.match(r"CALL P/IN\b", a.text)
                                        or re.match(r"eo[lf]", a.text)):
                out.append(a)
                out.append(Text(f"{'ifnot' if br == 'UZA' else 'ifgoto'} {b.arg}"))
                i += 2
                continue
        out.append(a)
        i += 1
    return out


def recognize_casts(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Fixed instruction idioms for type conversions -> named markers."""
    out: list[Node] = []
    i = 0
    seq = nodes
    def at(k, op, arg=None):
        n = seq[i + k] if i + k < len(seq) else None
        return is_insn(n, op, "") and (arg is None or n.arg == arg)
    while i < len(seq):
        if at(0, "NTR", "0"):
            if at(1, "AVX", "0"):
                out.append(Text("toReal")); i += 2; continue
            out.append(Text("toReal")); i += 1; continue
        if at(0, "APX", "p77777") and at(1, "ASN", "64+33") and at(2, "AEX", "int(0)"):
            out.append(Text("mapAI")); i += 3; continue
        if at(0, "A+X", "half") and at(1, "NTR", "7") and at(2, "A+X", "int(0)"):
            out.append(Text("round")); i += 3; continue
        out.append(seq[i]); i += 1
    return out


def convert_calls(d: Dialect, nodes: list[Node]) -> list[Node]:
    """`<call_reg>,VJM,target` -> `CALL target`, absorbing a trailing frame-
    restore (`<call_reg>,VJM,P/dd`) or display-restore (`<mtj>,MTJ,d`)."""
    mtj = "7" if d.call_reg == "13" else "15"
    out: list[Node] = []
    i = 0
    while i < len(nodes):
        a = nodes[i]
        if is_insn(a, "VJM", d.call_reg):
            b = nodes[i + 1] if i + 1 < len(nodes) else None
            consume = 1
            if is_insn(b, "VJM", d.call_reg) and b.arg.startswith("P/"):
                consume = 2  # scope-crossing call + frame restore
            elif is_insn(b, "MTJ", mtj):
                consume = 2
            out.append(Text(f"CALL {a.arg}"))
            i += consume
            continue
        out.append(a)
        i += 1
    return out


def indirect_addressing(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Resolve the WTC/UTC index idioms and address-of reads:
      `,WTC,x ; Y`        -> `Y[x]`              (subscript; repeated)
      `,UTC,x ; r,op,z`   -> `r,op,*(&x+z)`      (indexed indirect)
      `14,VTM,x ; ,ITA,14`-> `,XTA,&x`  (ITS -> XTS) (address-of)
      `,XTA,int(N) ; ,ATI,r` -> `r,VTM,N`        (set register indirectly)
    """
    # Each idiom is a separate full pass (as in the Perl's separate s///g), so
    # one pass's output can feed the next -- e.g. UTC builds `14,VTM,*(&x+0)`
    # which the address-of pass then pairs with the following `,ITA,14`.
    def wtc(ns):
        out, i, changed = [], 0, False
        while i < len(ns):
            a, b = ns[i], (ns[i + 1] if i + 1 < len(ns) else None)
            if is_insn(a, "WTC") and a.reg == "" and b is not None:
                if isinstance(b, Insn):
                    b = replace(b, arg=f"{b.arg}[{a.arg}]")
                else:
                    b = Text(f"{to_line(b)}[{a.arg}]")
                out.append(b); i += 2; changed = True
                continue
            out.append(a); i += 1
        return out, changed

    def pair(ns, fn):
        out, i = [], 0
        while i < len(ns):
            a, b = ns[i], (ns[i + 1] if i + 1 < len(ns) else None)
            rep = fn(a, b)
            if rep is not None:
                out.extend(rep); i += 2
            else:
                out.append(a); i += 1
        return out

    changed = True
    while changed:
        nodes, changed = wtc(nodes)

    # UTC indexed indirect: `,UTC,x ; r,op,z` -> `r,op,*(&x+z)`
    nodes = pair(nodes, lambda a, b: [replace(b, arg=f"*(&{a.arg}+{b.arg})")]
                 if is_insn(a, "UTC") and a.reg == "" and isinstance(b, Insn) else None)
    # address-of: `14,VTM,x ; ,ITA/ITS,14` -> `,XTA/XTS,&x`  (reg # is in operand)
    nodes = pair(nodes, lambda a, b: [Insn(op="XTA" if b.op == "ITA" else "XTS", arg=f"&{a.arg}")]
                 if is_insn(a, "VTM", "14") and is_insn(b) and b.op in ("ITA", "ITS")
                 and b.reg == "" and b.arg == "14" else None)
    # set register indirectly: `,XTA,int(N) ; ,ATI,r` -> `r,VTM,N`
    nodes = pair(nodes, lambda a, b: [Insn(op="VTM", reg=b.arg, arg=a.arg[4:-1])]
                 if is_insn(a, "XTA") and re.fullmatch(r"int\(\d+\)", a.arg)
                 and is_insn(b, "ATI") else None)
    return nodes


# DMS (decomp1) runtime write/io entry points, keyed by the P/ suffix.
_WRITE_NAME = {"7A": "writeString", "WI": "writeInt", "WL": "writeLN",
               "CW": "writeChar", "WC": "writeCharWide"}
_WRITE_IO = {"EO": "eof", "EL": "eoln", "GF": "get", "PF": "put",
             "RF": "reset", "TF": "rewrite"}


def _is_output_vtm(n: Node) -> bool:
    return is_insn(n, "VTM", "12") and "OUTPUT" in n.arg


def recognize_writes(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Recognize the Pascal I/O runtime calls (`<call>,VJM,P/<code>`):

      [12,VTM,OUTPUT;] P/7A/WI/WL/CW/WC -> writeString/Int/LN/Char/CharWide
      10,VTM,N; [12,VTM,OUTPUT;] P/6A   -> writeAlfa<N>
      P/WOLN                            -> writeLN
      12,VTM,X; P/EO/EL/GF/PF/RF/TF     -> eof/eoln/get/put/reset/rewrite(X)

    The argument-supplying `VTM`s precede the call, so we pop them back off the
    already-emitted output.  (Names are the decomp1/DMS and Pascal-Monitor set,
    via call_reg=13; Pascal-Autocode uses different P/-codes and registers and
    is handled by recognize_writes_pa.)
    """
    if d.track_regs:
        return nodes
    call = d.call_reg
    out: list[Node] = []
    for n in nodes:
        if is_insn(n, "VJM", call) and n.arg.startswith("P/"):
            code = n.arg[2:].strip()
            if code == "WOLN":
                out.append(Text("writeLN")); continue
            if code in _WRITE_IO and out and is_insn(out[-1], "VTM", "12"):
                out.append(Text(f"{_WRITE_IO[code]}({out.pop().arg})")); continue
            if code == "6A":
                if out and _is_output_vtm(out[-1]):
                    out.pop()
                if out and is_insn(out[-1], "VTM", "10"):
                    out.append(Text(f"writeAlfa{out.pop().arg}")); continue
                out.append(Text("writeAlfa")); continue
            if code in _WRITE_NAME:
                if out and _is_output_vtm(out[-1]):
                    out.pop()
                out.append(Text(_WRITE_NAME[code])); continue
        out.append(n)
    return out


# Pascal-Autocode (decomp2/3) I/O runtime entries, keyed by (register, P/code).
# writeString goes through reg 12 (runtime_reg); the rest through reg 14.
_PA_WRITE = {
    ("12", "7A"): "writeString", ("14", "WL"): "writeLN", ("14", "WOLN"): "writeLN",
    ("14", "CW"): "writeChar", ("14", "6A"): "CALL writeAlfa",
    ("14", "WI"): "CALL writeInt", ("14", "WC"): "CALL writeCharWide",
    ("14", "0026"): "get(input)", ("14", "0030"): "put(output)",
    ("14", "0033"): "CALL unpck", ("14", "0040"): "CALL put",
    ("14", "0041"): "CALL get", ("14", "0042"): "CALL reset",
    ("14", "0064"): "CALL rewrite",
}
_PA_IO = {"EO": "eof", "EL": "eoln", "GF": "get", "PF": "put",
          "RF": "reset", "TF": "rewrite"}


def recognize_writes_pa(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Pascal-Autocode I/O runtime calls -> named operators (decomp2/3).
    `12,VJM,P/7A`->writeString, `14,VJM,P/WL`->writeLN, ...; the file predicates
    `12,VTM,X ; 14,VJM,P/EO` -> eof(X) etc."""
    if not d.track_regs:
        return nodes
    out: list[Node] = []
    for n in nodes:
        if is_insn(n, "VJM") and n.arg.startswith("P/"):
            code = n.arg[2:].strip()
            name = _PA_WRITE.get((n.reg, code))
            if name is not None:
                out.append(Text(name)); continue
            if (n.reg == "14" and code in _PA_IO
                    and out and is_insn(out[-1], "VTM", "12")):
                out.append(Text(f"{_PA_IO[code]}({out.pop().arg})")); continue
        out.append(n)
    return out


def convert_new(d: Dialect, nodes: list[Node]) -> list[Node]:
    """`14,VTM,N ; <call>,VJM,P/NW ; ,ATX,X` -> `new(X=N)`."""
    call = d.call_reg
    out: list[Node] = []
    i = 0
    while i < len(nodes):
        a = nodes[i]
        b = nodes[i + 1] if i + 1 < len(nodes) else None
        c = nodes[i + 2] if i + 2 < len(nodes) else None
        if (is_insn(a, "VTM", "14") and is_insn(b, "VJM", call)
                and b.arg.startswith("P/NW") and is_insn(c, "ATX", "")):
            out.append(Text(f"new({c.arg}={a.arg})"))
            i += 3
            continue
        out.append(a)
        i += 1
    return out


def convert_goto(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Non-local GOTO via the P/RC runtime return-to-context entry:
        `<d>,MTJ,13 ; 14,VTM,X ; ,UJ,P/RC`  ->  `GOTO X`
    DMS/Pascal-Monitor carry X as a label already; Pascal-Autocode carries a
    numeric offset and renders it as an octal label `L%04o`.
    """
    out: list[Node] = []
    i = 0
    while i < len(nodes):
        a = nodes[i]
        b = nodes[i + 1] if i + 1 < len(nodes) else None
        c = nodes[i + 2] if i + 2 < len(nodes) else None
        if (is_insn(a, "MTJ") and a.reg.isdigit() and len(a.reg) == 1 and a.arg == "13"
                and is_insn(b, "VTM", "14")
                and is_insn(c, "UJ", "") and c.arg.rstrip() == "P/RC"):
            x = b.arg
            if d.track_regs and x.isdigit():
                out.append(Text(f"GOTO L{int(x):04o}"))
            else:
                out.append(Text(f"GOTO {x}"))
            i += 3
            continue
        out.append(a)
        i += 1
    return out


def remove_base_reset(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Drop base-register housekeeping after external calls (`8,BASE,x`,
    and decomp4's stray `,NTR,6`)."""
    out = []
    for n in nodes:
        if is_insn(n, "BASE", "8"):
            continue
        if d.label_style != "*" and is_insn(n, "NTR", "") and n.arg == "6":
            continue
        out.append(n)
    return out


def _hoist_headers(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Bring a routine's header up in front of its more-deeply-nested children
    (Perl's splice loop).  Marks bodies '(body)' and hoisted copies '(header)'.
    """
    nodes = list(nodes)
    mark = "" if d.label_style != "*" and not d.track_regs else " (header)"  # decomp4 unmarked
    curlev = 2
    i = 0
    while i < len(nodes):
        n = nodes[i]
        lvl = int(_HDR2.search(n.desc).group(1)) if isinstance(n, Header) and _HDR2.search(n.desc) else None
        if lvl is None or lvl == curlev:
            i += 1
            continue
        if curlev > lvl:
            n.role = " (body)"
            curlev = lvl
            i += 1
            continue
        for k in range(lvl - 1, curlev - 1, -1):
            found = next((nodes[j] for j in range(i + 1, len(nodes))
                          if isinstance(nodes[j], Header)
                          and _HDR2.search(nodes[j].desc)
                          and int(_HDR2.search(nodes[j].desc).group(1)) == k), None)
            if found is not None:
                nodes.insert(i, replace(found, role=mark))
        i += lvl - curlev
        curlev = lvl
    return nodes


# --------------------------------------------------------------------------- #
# Passes - the stack machine (slice 4)
# --------------------------------------------------------------------------- #

def to_line(n: Node) -> str:
    """Render a node in the Perl `@ops` string form the stack machine matches
    against (no leading space, no trailing `;`)."""
    if isinstance(n, Insn):
        lab = f"{n.label}:" if n.label is not None else ""
        return f"{lab}{n.reg},{n.op},{n.arg}"
    if isinstance(n, Text):
        return n.text
    if isinstance(n, Label):
        return f"{n.name}:,BSS,"
    if isinstance(n, Header):
        return f"{n.label_prefix}{n.off}: {n.desc}{n.role}"
    return n.render()


# A,E,R,O operand letters map to Pascal operators (Perl `tr/EROA/^$|&/`).
_AOP = {"E": "^", "R": "$", "O": "|", "A": "&", "-": "-", "+": "+", "*": "*", "/": "/"}

_RE_AX = re.compile(r"^,A([-+*/EROA])X,(.*)$")
_RE_15AX = re.compile(r"^15,A([-/EROA+*])X,0?$")
_RE_15C = re.compile(r"^15,C(..),0?$")
_RE_C = re.compile(r"^,C(..),(.*)$")


def stack_machine(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Emulate the accumulator/stack to rebuild expressions and statements.

    A faithful port of decomp4's interpreter (Pascal-Monitor); the decomp2/3
    register-tracking and `knargs` arity cases are enabled by the dialect.
    Comment nodes are hard barriers that flush the stack, preserving their
    flush-left rendering.  Output statements become Text nodes.
    """
    out: list[Node] = []
    stack: list[str] = []
    regs: dict[str, str] = {}
    yta_nonneg = ",YTA,(31C)" if d.track_regs else ",YTA,64"

    def dump():
        nonlocal stack
        if stack:
            out.append(Text("#" + " % ".join(stack)))
        stack = []

    # Work on a mutable list of (node, line) so passes that rewrite a *following*
    # line (Pascal-Autocode WTC indexing) can do so, like the Perl mutating @ops.
    items: list = [None if isinstance(n, Comment) else to_line(n) for n in nodes]
    comments = {i: n for i, n in enumerate(nodes) if isinstance(n, Comment)}

    i, N = 0, len(items)
    while i < N:
        if i in comments:
            dump(); out.append(comments[i]); i += 1; continue
        # Pascal-Autocode: a label opens a new basic block.  decomp3 discards
        # any leftover stack/regs as dead (`@stack = () if /:,/`) instead of
        # dumping `#...` -- the pre-label expression is recomputed inside the
        # block, so dumping it here is both spurious and blocks for-loop
        # recognition (it splits the init from its loop label).
        if d.track_regs and isinstance(nodes[i], Label):
            stack = []; regs = {}
            out.append(nodes[i]); i += 1; continue
        line = items[i]

        # ---- unary operators / conversions consuming the top of stack ----
        if stack and "CALL P/MI" in line:
            stack[-1] = f"mulFix({stack[-1]})"; i += 1; continue
        if stack and line == yta_nonneg:
            stack[-1] = f"nonNeg({stack[-1]})"; i += 1; continue
        if stack and d.track_regs and line == ",NTR,3":
            stack[-1] = f"disNorm({stack[-1]})"; i += 1; continue
        if stack and "CALL P/SS" in line:
            stack[-1] = f"toSet({stack[-1]})"; i += 1; continue
        if stack and "CALL P/TR" in line:
            stack[-1] = f"trunc({stack[-1]})"; i += 1; continue
        if stack and re.fullmatch(r"toBool|toReal|invBool|mapAI|round|LN|SQRT", line):
            stack[-1] = f"{line}({stack[-1]})"; i += 1; continue
        if re.match(r"^eo[lf]", line):
            if stack:
                stack[-1] = line
            else:
                stack = [line]
            i += 1; continue

        # ---- binary operators consuming two stack slots ----
        bin2 = None
        if stack and len(stack) >= 2:
            if "CALL P/IN" in line:
                inkw = "_IN" if d.underscore_kw else "IN"
                bin2 = (f"({stack[-2]} {inkw} {stack[-1]})" if d.track_regs
                        else f"({stack[-1]} {inkw} {stack[-2]})")
            elif "CALL P/PI" in line:
                bin2 = f"toRange({stack[-2]}..{stack[-1]})"
            elif "CALL P/DI" in line:
                bin2 = f"({stack[-2]} DIV {stack[-1]})"
            elif "CALL P/IS" in line:
                bin2 = f"({stack[-2]} /int/ {stack[-1]})"
            elif "CALL P/MD" in line or (d.track_regs and "12,VJM,P/MD" in line):
                modkw = "_MOD" if d.underscore_kw else "MOD"
                bin2 = f"({stack[-2]} {modkw} {stack[-1]})"
            elif d.track_regs and "realLT" in line:
                bin2 = f"({stack[-2]} < {stack[-1]})"
            elif d.track_regs and "realGT" in line:
                bin2 = f"({stack[-2]} > {stack[-1]})"
            elif d.track_regs and "realEQ" in line:
                bin2 = f"({stack[-2]} = {stack[-1]})"
        if bin2 is not None:
            stack[-2] = bin2; stack.pop(); i += 1; continue

        # ---- more unary operators ----
        if stack and (",AVX,int(-1)" in line or (d.track_regs and ",AVX,C/0043" in line)):
            stack[-1] = f"neg({stack[-1]})"; i += 1; continue
        if stack and ",AMX,0" in line:
            stack[-1] = f"abs({stack[-1]})"; i += 1; continue
        if stack and ",ACX,0" in line:
            stack[-1] = f"card({stack[-1]})"; i += 1; continue
        if stack and (",ANX,int(0)" in line or (d.track_regs and ",ANX,0" in line)):
            stack[-1] = f"ffs({stack[-1]})"; i += 1; continue
        if d.track_regs and stack and ",AEX,(1C)" in line:
            stack[-1] = f"_not ({stack[-1]})"; i += 1; continue
        if d.track_regs and stack and "CALL P/0024" in line:
            stack[-1] = f"intToReal({stack[-1]})"; i += 1; continue
        if d.track_regs and "CALL P/0023" in line:
            stack.append("pck"); i += 1; continue

        # ---- Pascal-Autocode register tracking ----
        if d.track_regs:
            m = re.match(r"(\d+),VTM,(.+)$", line)
            if m:
                regs[m.group(1)] = f"&{m.group(2)}"
                out.append(Text(f"R{m.group(1)} := &{m.group(2)}")); i += 1; continue
            m = re.match(r",ITA,(.+)$", line)
            if m:
                v = regs.get(m.group(1), f"R({m.group(1)})")
                if stack:
                    stack[-1] = v
                else:
                    stack.append(v)
                i += 1; continue
            m = re.match(r",ITS,(.+)$", line)
            if m:
                stack.append(regs.get(m.group(1), f"R({m.group(1)})")); i += 1; continue
            m = re.match(r",ASN,64([-+])(\d+)", line)
            if stack and m:
                stack[-1] = (f"({stack[-1]} << {m.group(2)})" if m.group(1) == "-"
                             else f"({stack[-1]} >> {m.group(2)})")
                i += 1; continue
            if len(stack) >= 2 and "15,WTC,0" in line and i + 1 < N and items[i + 1]:
                items[i + 1] += f"[{stack[-2]}]"
                stack[-2] = stack[-1]; stack.pop(); continue

        # ---- ,ATI,r : register assignment / capture ----
        m = re.match(r",ATI,(\d+)", line)
        if stack and m:
            if d.track_regs:
                regs[m.group(1)] = stack[-1]
                if len(stack) == 1:
                    stack = []
            else:
                stack[-1] = f"{{R{m.group(1)}={stack[-1]}}}"
            i += 1; continue

        # ---- a call / write consumes the whole stack as its arguments ----
        if stack and ("CALL " in line or line.startswith("write")):
            if d.known_args:
                consumed = _call_known(line)
            else:
                consumed = None
            if consumed is not None:
                args = ", ".join(stack[len(stack) - consumed:]) if consumed else ""
                out.append(Text(f"{line}( {args} )" if consumed else f"{line}()"))
                del stack[len(stack) - consumed:]
            else:
                out.append(Text(f"{line}( {', '.join(stack)} )"))
                stack = []
            i += 1; continue

        # ---- 15,A?X,0 : binary arithmetic on the two top slots ----
        m = _RE_15AX.match(line)
        if len(stack) >= 2 and m:
            op = _AOP[m.group(1)]
            stack[-2] = f"({stack[-1]} {op} {stack[-2]})"; stack.pop(); i += 1; continue
        m = _RE_15C.match(line)
        if len(stack) >= 2 and m:
            stack[-2] = f"({stack[-1]} {m.group(1)} {stack[-2]})"; stack.pop(); i += 1; continue
        m = _RE_C.match(line)
        if stack and m:
            stack[-1] = f"({stack[-1]} {m.group(1)} {m.group(2)})"; i += 1; continue
        m = re.match(r"^ifgoto (.*)", line)
        if stack and m:
            if d.underscore_kw:   # Pascal-Autocode keyword style (decomp3)
                out.append(Text(f"_if {stack[-1]} _then goto {m.group(1)}"))
            else:
                out.append(Text(f"if {stack[-1]} goto {m.group(1)}"))
            if len(stack) == 1:
                stack = []
            i += 1; continue
        m = re.match(r"^ifnot (.*)", line)
        if stack and m:
            if d.underscore_kw:
                out.append(Text(f"_if {stack[-1]} _then below _else goto {m.group(1)}"))
            else:
                out.append(Text(f"if not {stack[-1]} goto {m.group(1)}"))
            if len(stack) == 1:
                stack = []
            i += 1; continue
        m = re.match(r"^caseto (.*)", line)
        if stack and m:
            out.append(Text(f"case {stack[-1]} at {m.group(1)}"))
            if len(stack) == 1:
                stack = []
            i += 1; continue

        # ---- barrier: anything not a stack op (or a label while stacked) ----
        if not re.search(r",[ASX].[ASX],", line) or (stack and ":" in line):
            dump()
            out.append(_as_node(nodes[i], line))
            i += 1; continue

        # ---- value producers / consumers ----
        if line == "15,XTA,3":
            if stack:
                stack[-1] = "FUNCRET"
            else:
                stack = ["FUNCRET"]
            i += 1
        elif (m := re.match(r"^,XTA,(.*)", line)):
            if stack:
                stack[-1] = m.group(1)
            else:
                stack = [m.group(1)]
            i += 1
        elif re.match(r"^15,XTA,0?$" if d.track_regs else r"^15,XTA,$", line):
            i += 1
            if stack:
                stack.pop()
            else:
                out.append(Text(f"!!! Popping empty stack at {i}"))
        elif stack and (m := _RE_AX.match(line)):
            stack[-1] = f"({stack[-1]} {_AOP[m.group(1)]} {m.group(2)})"; i += 1
        elif stack and (m := re.match(r"^,X-A,(.*)", line)):
            stack[-1] = f"({m.group(1)} - {stack[-1]})"; i += 1
        elif stack and (m := re.match(r"^,XTS,(.*)", line)):
            stack.append(m.group(1)); i += 1
        elif stack and line == "15,ATX,0":
            stack.append(stack[-1]); i += 1
        elif stack and (m := re.match(r"^,ATX,(.*)", line)):
            out.append(Text(f"{m.group(1)} := {stack[-1]}"))
            if len(stack) == 1:
                stack = []
            i += 1
        elif stack and (m := re.match(r"^,STX,(.*)", line)):
            out.append(Text(f"{m.group(1)} := {stack[-1]}")); stack.pop(); i += 1
        else:
            dump()
            out.append(_as_node(nodes[i], line))
            i += 1
    dump()
    return out


def _as_node(orig: Node, line: str) -> Node:
    """Re-wrap an unrecognized line, preserving Label/Header identity so they
    keep their surface form; everything else becomes a Text statement."""
    if isinstance(orig, (Label, Header)) and to_line(orig) == line:
        return orig
    return Text(line)


def _call_known(line: str) -> Optional[int]:
    """decomp2/3 `knargs`: how many stack slots a CALL consumes, if known.
    Without a routines table loaded this is unknown -> None (consume all)."""
    return None


# --------------------------------------------------------------------------- #
# Passes - back-end substitution + render (slice 5)
#
# After the stack machine the program is statement text (Text nodes); these
# passes are string rewrites on that text plus a couple of cross-node merges.
# --------------------------------------------------------------------------- #

def _map_text(nodes: list[Node], fn: Callable[[str], str]) -> list[Node]:
    for n in nodes:
        if isinstance(n, Text):
            n.text = fn(n.text)
    return nodes


def convert_int_set(octal: str) -> str:
    """Interpret an octal literal as a 48-bit big-endian set and list members.
    `777777` -> `[30,31,...,47]`; `360000000000` -> `[13,14,15,16]`."""
    bits = "".join(format(int(c, 8), "03b") for c in octal).zfill(48)
    return "[" + ",".join(str(i) for i in range(48) if bits[i] == "1") + "]"


def simple_ops(d: Dialect, nodes: list[Node]) -> list[Node]:
    """`,UJ,P/E` -> RETURN (EXIT in the underscore-keyword dialect); drop
    post-call stack corrections `15,UTM,3|4`."""
    out: list[Node] = []
    for n in nodes:
        if isinstance(n, Text):
            t = n.text.rstrip()
            if re.fullmatch(r",UJ,P/E", t):
                out.append(Text("EXIT" if d.underscore_kw else "RETURN")); continue
            if re.fullmatch(r"15,UTM,[34]", t):
                continue
        out.append(n)
    return out


def convert_setup_rollup(d: Dialect, nodes: list[Node]) -> list[Node]:
    """decomp1/4: the global slot `<prefix>23z` is the heap top; reading it is a
    `setup(...)` and writing it a `rollup(...)`."""
    if d.track_regs:
        return nodes
    g23 = re.escape(f"{d.global_prefix}23z")
    for n in nodes:
        if isinstance(n, Text):
            mr = re.fullmatch(rf"(.+) := {g23}", n.text)
            mw = re.fullmatch(rf"{g23} := (.+)", n.text)
            if mr:
                n.text = f"setup({mr.group(1)})"
            elif mw:
                n.text = f"rollup({mw.group(1)})"
    return nodes


# GOST-8859 character table (decomp2/3), for single-char writes.
_GOSTTAB = (
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "+", "-", "/", ",", ".", " ",
    "⏨", "@", "(", ")", "×", "=", ";", "[", "]", "*", "`", "'", "#", "<", ">", ":",
    "A", "Б", "В", "Г", "Д", "Е", "Ж", "З", "И", "Й", "К", "Л", "М", "Н", "О", "П",
    "Р", "С", "Т", "У", "Ф", "Х", "Ц", "Ч", "Ш", "Щ", "Ы", "Ь", "Э", "Ю", "Я", "D",
    "F", "G", "I", "J", "L", "N", "Q", "R", "S", "U", "V", "W", "Z", "^", "?", "?",
    "?", "&", "?", "~", "?", "?", "%", "$", "|", "?", "_", "!", '"', "Ъ", "?", "\\",
)


def _gost_char(code: int) -> str:
    return _GOSTTAB[code] if code < 0o140 else f"_{code:03o}"


def _build_gost_map(nodes: list[Node]) -> dict[int, str]:
    """Map data address -> the GOST literal text there, from `/N:` + `,GOST,|s|`."""
    gmap: dict[int, str] = {}
    for k in range(len(nodes) - 1):
        lab = nodes[k]
        if isinstance(lab, Label) and lab.name.startswith("/") and lab.name[1:].isdigit():
            m = re.search(r",GOST, *\|([^|]+)\|", to_line(nodes[k + 1]))
            if m:
                gmap[int(lab.name[1:])] = m.group(1)
    return gmap


def _getstring(gmap: dict[int, str], addr: int, length: int) -> str:
    """Concatenate `length` bytes of the GOST string starting at word `addr`
    (faithful to the Perl: 6 chars per word, trailing trim via negative slice)."""
    s, a, n = "", addr, length
    while n > 0:
        if a not in gmap:
            return f"string({addr}, {length})"
        s += gmap[a]
        a += 1
        n -= 6
    return s if n == 0 else s[:n]


def convert_write_strings(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Pascal-Autocode string/char write operators (decomp2/3, end of pipeline):

      R10 := &-LEN ; R13 := &ADDR ; writeString  -> write('<string>')
      R13 := &ADDR ; 10,VJM,P/0066                -> BIND('<string>')
      writeInt( (NC), X )                         -> write(X[:N])
      writeCharWide( (NC), (MC) )                 -> write('<gost(M)>':N)
      output@ := (NC) ; put(output)               -> write('<gost(N)>')
    """
    if not d.track_regs:
        return nodes
    gmap = _build_gost_map(nodes)

    def txt(n):
        return n.text if isinstance(n, Text) else None

    out: list[Node] = []
    i = 0
    while i < len(nodes):
        a = nodes[i]
        b = nodes[i + 1] if i + 1 < len(nodes) else None
        c = nodes[i + 2] if i + 2 < len(nodes) else None
        e = nodes[i + 3] if i + 3 < len(nodes) else None
        ta, tb, tc, te = txt(a), txt(b), txt(c), txt(e)
        # write(file,'string':width)  via P/0071
        m1 = re.fullmatch(r"R10 := &(\d+)", ta) if ta else None
        m2 = re.fullmatch(r"R9 := &(\d+)", tb) if tb else None
        m3 = re.fullmatch(r"R13 := &([^;]+)", tc) if tc else None
        m4 = re.fullmatch(r"P/0071\( &(\d+), &\d+ \)", te) if te else None
        if m1 and m2 and m3 and m4:
            s = _getstring(gmap, int(m2.group(1)), int(m1.group(1)))
            out.append(Text(f"write({m3.group(1)},'{s}':{int(m4.group(1), 8)})"))
            i += 4
            continue
        # write('string':width)  via P/A7
        mw = re.fullmatch(r"P/A7\( \((\d+)C\), \(\d+C\) \)", tc) if tc else None
        if m1 and m2 and mw:
            s = _getstring(gmap, int(m2.group(1)), int(m1.group(1)))
            out.append(Text(f"write('{s}':{int(mw.group(1), 8)})"))
            i += 3
            continue
        # write('string')  via writeString
        m1 = re.fullmatch(r"R10 := &-(\d+)", ta) if ta else None
        m2 = re.fullmatch(r"R13 := &(\d+)", tb) if tb else None
        if m1 and m2 and tc == "writeString":
            out.append(Text(f"write('{_getstring(gmap, int(m2.group(1)), int(m1.group(1)))}')"))
            i += 3
            continue
        # BIND('string')
        m = re.fullmatch(r"R13 := &(\d+)", ta) if ta else None
        if m and tb == "10,VJM,P/0066":
            out.append(Text(f"BIND('{_getstring(gmap, int(m.group(1)), 6)}')"))
            i += 2
            continue
        # output@ := (NC) ; put(output)  ->  write('<char>')
        mo = re.fullmatch(r"output@ := \((\d+)C\)", ta) if ta else None
        if mo and tb == "put(output)":
            out.append(Text(f"write('{_gost_char(int(mo.group(1), 8))}')"))
            i += 2
            continue
        # single-statement forms
        if ta is not None:
            new = _rewrite_write_stmt(ta)
            if new is not None:
                out.append(Text(new)); i += 1; continue
        out.append(a)
        i += 1
    return out


def _rewrite_write_stmt(t: str) -> Optional[str]:
    m = re.fullmatch(r"writeInt\( \((\d+)C\), (.+) \)", t)
    if m:
        w = "" if int(m.group(1)) == 1 else f":{int(m.group(1))}"
        return f"write({m.group(2)}{w})"
    m = re.fullmatch(r"writeCharWide\( \((\d+)C\), \((\d+)C\) \)", t)
    if m:
        return f"write('{_gost_char(int(m.group(2), 8))}':{int(m.group(1), 8)})"
    return None


def convert_sets(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Octal set literals -> member lists, by context:
    ` IN (NC)`->` IN [set]`, ` | (NC)`->` + [set]`, ` & (NC)`->` * [set]`,
    plus the `((NC) ^ allones) & X` set-complement idioms."""
    inkw = "_IN" if d.underscore_kw else "IN"
    def sub(t: str) -> str:
        t = re.sub(rf" {inkw} \(([0-7]+)C\)",
                   lambda m: f" {inkw} " + convert_int_set(m.group(1)), t)
        t = re.sub(r" \| \(([0-7]+)C\)", lambda m: " + " + convert_int_set(m.group(1)), t)
        t = re.sub(r" \& \(([0-7]+)C\)", lambda m: " * " + convert_int_set(m.group(1)), t)
        t = re.sub(r"(if[^;]*)\(\(\(([0-7]+)C\) \^ allones\) \& ([^()]+)\)",
                   lambda m: f"{m.group(1)} not ({m.group(3)} <= {convert_int_set(m.group(2))})", t)
        t = re.sub(r"\(\(\(([0-7]+)C\) \^ allones\) \& ([^()]+)\)",
                   lambda m: f"({m.group(2)} - {convert_int_set(m.group(1))})", t)
        return t
    return _map_text(nodes, sub)


_RELOP = {" EQ ": " = ", " NE ": " <> ", " LT ": " < ",
          " LE ": " <= ", " GT ": " > ", " GE ": " >= "}


def convert_relops(d: Dialect, nodes: list[Node]) -> list[Node]:
    return _map_text(nodes, lambda t: re.sub(
        r" EQ | NE | LT | LE | GT | GE ", lambda m: _RELOP[m.group(0)], t))


def struct_fields(d: Dialect, nodes: list[Node]) -> list[Node]:
    """`N[expr]` -> `expr@.f[N]` (record field access via word offset)."""
    return _map_text(nodes, lambda t: re.sub(
        r"(\d+)\[([^\[\]]+)\]", r"\2@.f[\1]", t))


def funcret_and_calls(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Merge a `CALL f` with the following statement that consumes its result
    (`x := FUNCRET` -> `x := f`), then strip the residual `CALL ` keyword."""
    out: list[Node] = []
    i = 0
    while i < len(nodes):
        a = nodes[i]
        b = nodes[i + 1] if i + 1 < len(nodes) else None
        if (isinstance(a, Text) and isinstance(b, Text)
                and a.text.startswith("CALL") and "FUNCRET" in b.text):
            call = a.text[len("CALL"):]
            before, _, after = b.text.partition("FUNCRET")
            out.append(Text(f"{before} {call} {after}".strip()))
            i += 2
            continue
        out.append(a)
        i += 1
    return _map_text(out, lambda t: t.replace("CALL ", ""))


def stray_atx_assign(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Pascal-Autocode: a leftover `,ATX,X` the stack machine couldn't pair
    (empty accumulator) becomes an empty-RHS assignment `X := ` (decomp3)."""
    if not d.underscore_kw:
        return nodes
    return _map_text(nodes, lambda t: re.sub(r",ATX,([^;]+)", r"\1 := ", t))


def recognize_for_loops(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Pascal-Autocode: fold a counted loop into a `_for` (decomp3 l.823):

        VAR := START ; L:,BSS, ; _if (VAR op END) _then goto Lexit ;
          BODY ; VAR := (VAR +/- (1C)) ; ,UJ,L
        -> _for VAR := START {_to|_downto} END _do _( ; BODY ; _)

    `op` `>` -> `_to` (count up), `<` -> `_downto`.  Each loop is anchored on its
    own back-jump `,UJ,L` and paired to label L by name, so nested loops resolve
    innermost-first (fixpoint).  The exit label is NOT required to follow the
    back-jump, so loops that exit to a shared address are still recognized; it is
    left in place rather than consumed.
    """
    if not d.underscore_kw:
        return nodes
    changed = True
    while changed:
        changed = False
        label_at = {n.name: k for k, n in enumerate(nodes) if isinstance(n, Label)}
        for bj, n in enumerate(nodes):
            mbj = isinstance(n, Text) and re.fullmatch(r",UJ,(L\d+)", n.text)
            if not mbj:
                continue
            li = label_at.get(mbj.group(1))
            if li is None or not (1 <= li and li + 2 <= bj - 1):
                continue
            init, guard, incr = nodes[li - 1], nodes[li + 1], nodes[bj - 1]
            if not (isinstance(init, Text) and isinstance(guard, Text)
                    and isinstance(incr, Text)):
                continue
            mi = re.fullmatch(r"(.+) := (.+)", init.text)
            if not mi:
                continue
            var, start = mi.group(1), mi.group(2)
            v = re.escape(var)
            if not re.fullmatch(rf"{v} := \({v} [-+] \(1C\)\)", incr.text):
                continue
            mg = re.fullmatch(rf"_if \({v} ([<>]) (.+)\) _then goto L\d+", guard.text)
            if not mg:
                continue
            kw = "_to" if mg.group(1) == ">" else "_downto"
            head = Text(f"_for {var} := {start} {kw} {mg.group(2)} _do _(")
            body = nodes[li + 2:bj - 1]
            close = Text(f" (* for {mbj.group(1)[1:]} *) _)")  # label number, no 'L'
            nodes = nodes[:li - 1] + [head] + body + [close] + nodes[bj + 1:]
            changed = True
            break
    return nodes


def recognize_while_loops(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Pascal-Autocode: fold a pre-tested loop into a `_while` (decompG l.938):

        L1:,BSS, ; _if (COND) _then goto L2 ; BODY ; ,UJ,L1 ; L2:,BSS,
        -> L1:,BSS, ; _while _not (COND) _do _( ; BODY ; (* while L1 *) _) ; L2:

    The guard tests the *exit* condition at the top, so the loop runs while it is
    false -> `_while _not COND`.  Like `recognize_for_loops`, each loop is anchored
    on its own back-jump `,UJ,L1` (paired to L1 by name, fixpoint for nesting), but
    -- faithful to decompG -- the exit label L2 must immediately follow the
    back-jump and match the guard's target.  Runs after `recognize_for_loops`, so a
    counted loop's back-jump is already consumed and never mistaken for a `_while`.
    """
    if not d.underscore_kw:
        return nodes
    changed = True
    while changed:
        changed = False
        label_at = {n.name: k for k, n in enumerate(nodes) if isinstance(n, Label)}
        for bj, n in enumerate(nodes):
            mbj = isinstance(n, Text) and re.fullmatch(r",UJ,(L\d+)", n.text)
            if not mbj:
                continue
            top = mbj.group(1)
            li = label_at.get(top)
            if li is None or li + 1 > bj - 1:
                continue
            guard = nodes[li + 1]
            mg = (isinstance(guard, Text)
                  and re.fullmatch(r"_if (.+) _then goto (L\d+)", guard.text))
            if not mg:
                continue
            exit_lbl = mg.group(2)
            if not (bj + 1 < len(nodes) and isinstance(nodes[bj + 1], Label)
                    and nodes[bj + 1].name == exit_lbl):
                continue
            head = Text(f"_while _not {mg.group(1)} _do _(")
            body = nodes[li + 2:bj]
            close = Text(f" (* while {top} *) _)")
            nodes = (nodes[:li + 1] + [head] + body + [close] + nodes[bj + 1:])
            changed = True
            break
    return nodes


def _ifgoto(n: Node):
    """`(between, target)` for a Text `_if {between}goto {target}` (a forward
    branch the stack machine emitted), else None.  `between` is everything from
    after `_if ` up to `goto`, keeping its trailing space -- e.g. `(c) _then ` or
    the `_then below _else` form `(c) _then below _else `."""
    if isinstance(n, Text) and not n.raw:
        m = re.fullmatch(r"_if (.*)goto (L\d+)", n.text)
        if m:
            return m.group(1), m.group(2)
    return None


def _or_guard(n: Node):
    """`(cond, target)` for a *plain* `_if {cond} _then goto {target}` (no
    `below`/`_else`), the only shape decompG's `_or` merge folds.  `cond` is the
    condition alone, without the surrounding `_then`."""
    if isinstance(n, Text) and not n.raw:
        m = re.fullmatch(r"_if (.+) _then goto (L\d+)", n.text)
        if m:
            return m.group(1), m.group(2)
    return None


def recognize_structured_ifs(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Pascal-Autocode: fold forward `_if ... _then goto L` branches into
    structured ifs, mirroring decompG ll.926/929/932 in three sub-passes:

      _or     `_if A _then goto L ; _if B _then goto L`
              -> `_if ( A _or  B) _then goto L`   (pairwise, left to right)
      single  `_if C _then goto L ; STMT ; L:`    (STMT one node, no 'J')
              -> `_if _not C _then  STMT ; L:`
      block   `_if C _then goto L ; BODY ; L:`    (BODY nodes free of 'B'/'S')
              -> `_if _not C _then  _( ; BODY ; _) ; L:`

    Runs after `recognize_while_loops`, so a loop's back-jump guard is already a
    `_while` and never seen here as a forward if.  Faithful to decompG: the `_or`
    merge is pairwise (a third same-target guard is left alone, as the Perl
    `s///g` resumes past the merged pair); single is tried before block; the body
    must sit directly before its exit label, which is kept in place.  The 'J' /
    'B'/'S' body screens are decompG's literal `[^J;]` / `[^BS]` char classes:
    'J' bars `,UJ,`/`,MTJ,`/`,VJM,`; 'B'/'S' bar a `,BSS,` label or `BIND` (and,
    as in the Perl, any ASCII 'B'/'S' inside a string literal)."""
    if not d.underscore_kw:
        return nodes

    # 1. _or merge (pairwise: consume both guards, advance past the merged node).
    out: list[Node] = []
    i = 0
    while i < len(nodes):
        a = _or_guard(nodes[i])
        b = _or_guard(nodes[i + 1]) if i + 1 < len(nodes) else None
        if a and b and a[1] == b[1]:
            out.append(Text(f"_if ( {a[0]} _or  {b[0]}) _then goto {a[1]}"))
            i += 2
        else:
            out.append(nodes[i])
            i += 1
    nodes = out

    # 2. single-statement if: one body node with no 'J' directly before exit L.
    out = []
    i = 0
    while i < len(nodes):
        g = _ifgoto(nodes[i])
        if g and i + 2 < len(nodes):
            between, target = g
            body, lbl = nodes[i + 1], nodes[i + 2]
            if (isinstance(body, Text) and not body.raw and "J" not in body.text
                    and isinstance(lbl, Label) and lbl.name == target):
                out.append(Text(f"_if _not {between} {body.text}"))
                i += 2          # body folded in; exit label processed next
                continue
        out.append(nodes[i])
        i += 1
    nodes = out

    # 3. block if: a run of 'B'/'S'-free body nodes ending exactly at exit L.
    out = []
    i = 0
    while i < len(nodes):
        g = _ifgoto(nodes[i])
        if g:
            between, target = g
            body: list[Node] = []
            found = False
            m = i + 1
            while m < len(nodes):
                nm = nodes[m]
                if isinstance(nm, Label) and nm.name == target:
                    found = bool(body)
                    break
                if (isinstance(nm, Text) and not nm.raw
                        and "B" not in nm.text and "S" not in nm.text):
                    body.append(nm)
                    m += 1
                    continue
                break               # B/S node, wrong label, or non-text: no block
            if found:
                out.append(Text(f"_if _not {between} _("))
                out.extend(body)
                out.append(Text("_)"))
                i = m               # exit label processed next
                continue
        out.append(nodes[i])
        i += 1
    return out


def _code_start(nodes: list[Node]) -> int:
    """The `C Code start: NNNN` octal threshold (decompG l.9), separating data
    addresses (`/N`) from code addresses (`LN`).  Defaults huge if absent so
    every address renders as `/N`."""
    for n in nodes:
        if isinstance(n, Comment):
            m = re.search(r"Code start:\s*(\d+)", n.text)
            if m:
                return int(m.group(1), 8)
    return 1 << 60


def pa_addr_folds(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Pascal-Autocode address folds that must run *before* structured-if
    recognition (decompG ll.913/917):

      `R13 := &6 ; writeAlfa( &6, X )`  ->  `write(X)`        (adjacent nodes)
      `R13 := &N`                       ->  `R13 := /N` | `LN`  (`section`)

    The `writeAlfa` fold precedes `section` because `section` would rewrite the
    `&6` it keys on.  `section` maps a numeric R13 target to a data label `/N`
    (N below the code-start address) or an octal code label `LN`."""
    if not d.underscore_kw:
        return nodes
    out: list[Node] = []
    i = 0
    while i < len(nodes):
        a, b = nodes[i], nodes[i + 1] if i + 1 < len(nodes) else None
        if (isinstance(a, Text) and not a.raw and a.text == "R13 := &6"
                and isinstance(b, Text) and not b.raw):
            mb = re.fullmatch(r"writeAlfa\( &6, (.+) \)", b.text)
            if mb:
                out.append(Text(f"write({mb.group(1)})"))
                i += 2
                continue
        out.append(a)
        i += 1
    nodes = out

    code = _code_start(nodes)

    def section(m: "re.Match[str]") -> str:
        val = int(m.group(1))
        return "R13 := /" + m.group(1) if val < code else f"R13 := L{val:o}"

    return _map_text(nodes, lambda t: re.sub(r"R13 := &(\d+)", section, t))


def pa_ptr_folds(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Pascal-Autocode pointer / I/O folds that run *after* structured-if
    recognition (decompG ll.936/948/951):

      `&*(&X+0)`  -> `X`                 (trivial pass-by-reference)
      `@.f[0]`    -> `@`                 (drop the synthetic first-field index)
      `output@ := X ; put(output)` -> `write(X)`   (adjacent nodes)
    """
    if not d.underscore_kw:
        return nodes
    nodes = _map_text(nodes, lambda t: re.sub(r"&\*\(&([a-z0-9]+)\+0\)", r"\1", t))
    nodes = _map_text(nodes, lambda t: t.replace("@.f[0]", "@"))

    out: list[Node] = []
    i = 0
    while i < len(nodes):
        a, b = nodes[i], nodes[i + 1] if i + 1 < len(nodes) else None
        if (isinstance(a, Text) and not a.raw and isinstance(b, Text)
                and not b.raw and b.text == "put(output)"):
            ma = re.fullmatch(r"output@ := (.*)", a.text)
            if ma:
                out.append(Text(f"write({ma.group(1)})"))
                i += 2
                continue
        out.append(a)
        i += 1
    return out


def cleanup_ws(d: Dialect, nodes: list[Node]) -> list[Node]:
    def sub(t: str) -> str:
        t = re.sub(r" +\( +", " (", t)
        t = re.sub(r" +\) +", ") ", t)
        return t.strip()
    return _map_text(nodes, sub)


def _mkargs(lev: int, n: int) -> str:
    return ", ".join(f"l{lev}a{k}z" for k in range(1, n + 1)) + ":integer"


def _mkvars(lev: int, n: int) -> str:
    if n == 0:
        return ""
    return "_var " + ", ".join(f"l{lev}v{k}z" for k in range(1, n + 1)) + ":integer;"


def emit_pascal_decls(d: Dialect, nodes: list[Node]) -> list[Node]:
    """decomp4: turn unambiguous headers into Pascal procedure/function/var
    declarations and `_(`, and the trailing `RETURN` before a separator -> `_)`.
    Ambiguous (`or a func`) headers are left as descriptive comments."""
    if not d.emit_pascal_decls:
        return nodes
    out: list[Node] = []
    for idx, n in enumerate(nodes):
        if isinstance(n, Header):
            name = f"{n.label_prefix}{n.off}"
            m0 = re.fullmatch(r"Level (\d) procedure with 0 arguments and (\d+) locals", n.desc)
            mp = re.fullmatch(r"Level (\d) procedure with (\d) arguments? and (\d+) locals", n.desc)
            mf = re.fullmatch(r"Level (\d) function with (\d) arguments? and (\d+) locals", n.desc)
            if m0:
                lev, nv = int(m0.group(1)), int(m0.group(2))
                out.append(Text(f"(* Level {lev} *) procedure {name};\n {_mkvars(lev, nv)} _("))
                continue
            if mp:
                lev, na, nv = int(mp.group(1)), int(mp.group(2)), int(mp.group(3))
                out.append(Text(f"(* Level {lev} *) procedure {name}({_mkargs(lev, na)});\n {_mkvars(lev, nv)} _("))
                continue
            if mf:
                lev, na, nv = int(mf.group(1)), int(mf.group(2)), int(mf.group(3))
                out.append(Text(f"(* Level {name} *) function {name}({_mkargs(lev, na)}):integer;\n {_mkvars(lev, nv)} _("))
                continue
        # RETURN immediately before a `==========` separator -> `_)`
        if (isinstance(n, Text) and n.text == "RETURN"
                and idx + 1 < len(nodes) and isinstance(nodes[idx + 1], Text)
                and nodes[idx + 1].text.startswith("=====")):
            out.append(Text("_)"))
            continue
        out.append(n)
    return out


def _mkargs_pa(lev: int, n: int) -> str:
    """decompG mkargs: `(l<lev>a1z, …:integer)`, empty for 0 args (the 0-arg
    header has no parameter list at all)."""
    if n == 0:
        return ""
    return "(" + ", ".join(f"l{lev}a{k}z" for k in range(1, n + 1)) + ":integer)"


def _mkvars_pa(lev: int, n: int) -> str:
    """decompG mkvars: `_var l<lev>v1z, …:integer;`, with locals beyond 100
    spilled into `l<lev>v101z: _array [101..n] _of integer;`."""
    if n == 0:
        return ""
    if n > 100:
        return _mkvars_pa(lev, 100) + f"l{lev}v101z: _array [101..{n}] _of integer;"
    return "_var " + ", ".join(f"l{lev}v{k}z" for k in range(1, n + 1)) + ":integer;"


def emit_pascal_decls_pa(d: Dialect, nodes: list[Node]) -> list[Node]:
    """Pascal-Autocode (decompG l.975): turn unambiguous procedure/function
    headers into `_proced`/`_function` declarations with underscore-keyword
    `_var`/`_array` lists and a `_(` block open.  Ambiguous `(or a func)`
    headers are left as-is (their regex doesn't match), matching decompG.  The
    runtime return is already `EXIT` by this point; here the final one before a
    `=====` separator closes the block as `_)`.
    """
    if not d.underscore_kw:
        return nodes
    out: list[Node] = []
    for idx, n in enumerate(nodes):
        # Only the forward declaration becomes a `_proced`/`_function`; the
        # in-place `(body)` occurrence stays a descriptive comment, as decompG's
        # `locals?;` regex won't match its `locals (body);` tail.
        if isinstance(n, Header) and n.role != " (body)":
            name = f"{n.label_prefix}{n.off}"
            m0 = re.fullmatch(r"Level (\d) procedure with 0 arguments and (\d+) locals", n.desc)
            mp = re.fullmatch(r"Level (\d) procedure with (\d) arguments? and (\d+) locals", n.desc)
            mf = re.fullmatch(r"Level (\d) function with (\d) arguments? and (\d+) locals", n.desc)
            if m0:
                lev, nv = int(m0.group(1)), int(m0.group(2))
                out.append(Text(f"(* Level {lev} *) _proced {name};\n{_mkvars_pa(lev, nv)} _(", raw=True))
                continue
            if mp:
                lev, na, nv = int(mp.group(1)), int(mp.group(2)), int(mp.group(3))
                out.append(Text(f"(* Level {lev} *) _proced {name}{_mkargs_pa(lev, na)};\n{_mkvars_pa(lev, nv)} _(", raw=True))
                continue
            if mf:
                lev, na, nv = int(mf.group(1)), int(mf.group(2)), int(mf.group(3))
                # `(* Level {name} *)` reproduces decompG's $1-vs-$2 quirk.
                out.append(Text(f"(* Level {name} *) _function {name}{_mkargs_pa(lev, na)}:integer;\n{_mkvars_pa(lev, nv)} _(", raw=True))
                continue
            # An unconverted (ambiguous `or a func`) forward declaration: decompG
            # renders every forward decl clean, so drop the ` (header)` marker.
            if n.role == " (header)":
                out.append(replace(n, role="")); continue
        # The runtime return is already `EXIT` (renamed up front); only the final
        # one before a `==========` separator collapses to the block close `_)`
        # (decompG l.979).  An `EXIT` folded into a structured if stays put.
        if (isinstance(n, Text) and n.text == "EXIT"
                and idx + 1 < len(nodes) and isinstance(nodes[idx + 1], Text)
                and nodes[idx + 1].text.startswith("=====")):
            out.append(Text("_)"))
            continue
        out.append(n)
    return out


# --------------------------------------------------------------------------- #
# Pipeline / Dialect
# --------------------------------------------------------------------------- #

# A pass is bound to its Dialect; the pipeline applies `p(d, nodes)`.
Pass = Callable[["Dialect", list[Node]], list[Node]]


@dataclass
class Dialect:
    """A target profile: the data that distinguishes decomp1..4.

    Three targets over two calling conventions: DMS (decomp1) and Pascal-Monitor
    (decomp4) both call via reg 13 / runtime via 14; Pascal-Autocode (decomp2/3)
    calls via reg 14 / runtime via 12.  DMS is told apart by its `*nnnn:` label
    style (the Pascal-exec targets use `Lnnnn:`).  Per-target name tables and
    pass toggles live here too, so the engine itself is shared.
    """
    name: str
    call_reg: str           # index register carrying subroutine calls
    runtime_reg: str        # index register carrying P/n runtime prologue calls
    label_style: str = "L"  # 'L' -> Lnnnn: (Pascal-exec); '*' -> *nnnn: (DMS, decomp1)
    global_prefix: str = "g"     # named-global prefix: g/gl/glob per variant
    track_regs: bool = False     # decomp2/3 register tracking in stack machine
    known_args: bool = False     # decomp2/3 knargs call-arity checking
    emit_pascal_decls: bool = False  # decomp4 procedure/var declaration emission
    underscore_kw: bool = False  # Pascal-Autocode style: _if/_then/_else, X := (empty RHS)
    const_map: dict[str, str] = field(default_factory=dict)


# Ordered pass list, shared by all dialects; family/target differences are
# expressed inside the passes via the Dialect, not by forking the pipeline.
PIPELINE: list[Pass] = [
    # --- slice 1: front-end normalization ---
    normalize_offsets,
    drop_empty_labels,
    drop_frame_restore,
    normalize_utm_wrap,
    for_loop_transform,
    normalize_global_refs,
    split_labels,
    # --- slice 2: prologue recognition + processprocs ---
    its11_prologue,
    recognize_prologues,
    vtm_uj_shortcut,
    global_via_reg1,
    global_via_p1d,
    normalize_utc_vtm,
    processprocs,
    # --- slice 3: pre-stack-machine recognizers ---
    subst_constants,
    indirect_addressing,
    recognize_writes,
    recognize_writes_pa,
    convert_new,
    convert_calls,
    remove_base_reset,
    convert_goto,
    recognize_casts,
    fold_branches,
    # --- slice 4: stack machine ---
    stack_machine,
    # --- slice 5: back-end substitution + render ---
    simple_ops,
    convert_setup_rollup,
    convert_sets,
    funcret_and_calls,
    convert_write_strings,   # after CALL-strip: writeInt(...)/P/A7(...) are clean
    struct_fields,
    convert_relops,
    cleanup_ws,
    recognize_for_loops,
    recognize_while_loops,
    pa_addr_folds,             # decompG writeAlfa fold + R13 section labels
    recognize_structured_ifs,  # decompG (Pascal-Autocode) forward-if folding
    pa_ptr_folds,              # decompG pointer / output@-put folds
    emit_pascal_decls,       # decomp4 (Pascal-Monitor) declarations
    emit_pascal_decls_pa,    # decompG (Pascal-Autocode) declarations + EXIT
    stray_atx_assign,
]


# Pre-seeded constants in the global area, by named-global offset.  Same set for
# DMS (decomp1) and Pascal-Monitor (decomp4); only the global prefix differs.
def _preseeded(prefix: str) -> dict[str, str]:
    vals = {8: "e1", 9: "00", 10: "multmask", 12: "mantissa", 15: "-1",
            17: "+1", 18: "p77777", 19: "real0_5", 20: "allones"}
    return {f"{prefix}{off}z": v for off, v in vals.items()}


# DMS (decomp1), Pascal-Monitor (decomp4), Pascal-Autocode (decomp2/3) base
# profiles.  Per-target tables (globals/routines/symbols) layer on from profiles/.
PROFILES: dict[str, Dialect] = {
    "1": Dialect(name="decomp1", call_reg="13", runtime_reg="14",  # DMS target
                 label_style="*", global_prefix="glob",
                 const_map=_preseeded("glob")),
    "4": Dialect(name="decomp4", call_reg="13", runtime_reg="14",
                 global_prefix="g", emit_pascal_decls=True,
                 const_map=_preseeded("g")),
    # Pascal-Autocode.  decomp2 is subsumed by decomp3 (its golden differs only
    # in keyword style, stray-ATX rendering, and weaker `put` recognition -- all
    # now adopted from decomp3), so only the canonical decomp3 profile is kept.
    "3": Dialect(name="decompG", call_reg="14", runtime_reg="12",
                 global_prefix="g", track_regs=True, known_args=True,
                 underscore_kw=True,
                 const_map={"g8z": "output@", "g7z": "input@", "C/0000": "allones"}),
}


def run(text: str, dialect: Dialect) -> str:
    nodes = tokenize(text)
    for p in PIPELINE:
        nodes = p(dialect, nodes)
    return render_out(nodes)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("input", nargs="?", default="-",
                    help="dtran -d listing, or '-' for stdin")
    ap.add_argument("--profile", default="4", choices=sorted(PROFILES),
                    help="dialect profile (1..4, mirroring decompN.pl)")
    ap.add_argument("--roundtrip", action="store_true",
                    help="tokenize then render with no passes (spine self-test)")
    args = ap.parse_args(argv)

    text = sys.stdin.read() if args.input == "-" else open(args.input, encoding="utf-8").read()

    if args.roundtrip:
        sys.stdout.write(render(tokenize(text)))
        return 0

    sys.stdout.write(run(text, PROFILES[args.profile]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
