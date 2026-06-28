# Should `dtran` become a front-end for Radare2, Ghidra, or IDA?

A design discussion about whether to convert the combined BESM-6 `dtran` disassembler into a
front-end (or processor module / loader) for one of the mainstream reverse-engineering
platforms — Radare2, Ghidra, or IDA Pro.

## 1. Reframing: `dtran` is only incidentally a disassembler

The opcode→mnemonic step (the `op[]` table + `prinsn`) is maybe 10% of `dtran`'s value. The
other 90% is BESM-6 / Pascal-Autocode / Dubna-Monitor-System domain reconstruction that a
generic RE tool gives you nothing for and that you would have to re-express anyway:

- three container formats (DMS object, Pascal exec A, Pascal exec B) + auto-detection;
- recursive-descent reachability to split code from data (`find_code_offset`, plus the
  jump-table / `check_chain` / `8,UZA` heuristics);
- literal-pool classification (GOST / ITM / ISO / TEXT / INT / LOG);
- symbol recovery — the DMS file symbol table, the Pascal-monitor symbol-table walk
  (`prsymtab`: routine names, line numbers, local variable layouts), the register
  save/restore stub patterns (`P/2..P/6`, `P/EF`, `P/E`, `P/1D`), and the hard-coded runtime
  routine names (`P/...`).

And the *output* is not standard assembly — it is a bespoke pseudo-assembly that feeds a
downstream decompiler (`decomp.pl`, the `-d` mode). So the real question is not "can these
tools disassemble BESM-6" but: **do you want a mainstream tool's machinery (decompiler, graph
view, cross-references, type system, GUI, scripting, longevity) instead of the bespoke
pipeline?**

## 2. Radare2 vs Ghidra

**Ghidra is the better target, not Radare2.**

- Radare2 is fundamentally byte-addressed and has no native decompiler (r2ghidra just routes
  back to Ghidra's). BESM-6 — a 48-bit, word-addressed machine with two 24-bit instructions
  per word — fights r2's model harder than Ghidra's, for less payoff.
- Ghidra supports configurable addressable-unit sizes and arbitrary varnode widths, and there
  is prior art for comparably exotic machines (the PDP-10 36-bit processor modules, various
  word-addressed DSPs).

If the goal is interactive RE plus a real decompiler, Ghidra is the target.

## 3. The crux risk: address-space modeling

This is inherent to the machine, not to any one tool. How do you represent 48-bit,
word-addressed memory with two 24-bit instructions per word?

- **Word as the addressable unit** (Ghidra `wordsize` = 6): addresses match BESM-6 word
  indices and data literals are clean, but you now need the decoder to split a 48-bit token
  into the left/right 24-bit instructions — and Ghidra strongly prefers one instruction per
  address, so two-per-word needs an awkward packed / sub-instruction scheme.
- **Half-word (24-bit) as the addressable unit**: maps cleanly to "one instruction per
  address" (and matches how `prsymtab` already walks left/right halves), but then 48-bit data
  words live at every *other* address, making the literal pools ugly.

There is no clean answer; whichever you pick, the SLEIGH semantics for a 48-bit
accumulator/stack machine with 48-bit floating point and the logical ops is the months-long
expert part. That is where the effort actually goes.

## 4. How `dtran`'s pieces would map onto Ghidra

| `dtran` component | Ghidra home |
|---|---|
| `op[]` + `prinsn` | SLEIGH `.slaspec` (decode + pcode semantics) |
| format detect + loaders | a `Loader` (Java): define the address space, lay down code/data |
| reachability / `code_map` | rely on Ghidra auto-analysis, or an analyzer that seeds entry points |
| symtab recovery, stub patterns, runtime names | analyzer scripts that apply symbols/labels |
| literal classification | data-type formats / a "find strings" analyzer per encoding |
| the bespoke decompiler | Ghidra's decompiler (the big potential win — or loss; see below) |

## 5. Two paths (Ghidra)

1. **Invest in a real module** if you want a better/maintained interactive tool: a Ghidra
   SLEIGH module + loader, reusing `dtran`'s reconstruction as a loader/analyzer rather than
   throwing it away. **Caveat:** Ghidra's *generic* decompiler may produce worse output than
   your bespoke one, which is presumably tuned to Pascal-Autocode idioms (the `P/` runtime
   calls, the IN-type call chains, etc.). You might gain navigation/types/GUI but regress on
   decompilation quality unless you also teach Ghidra those idioms.
2. **Keep your pipeline, add an export path** if you mostly want navigation/cross-references
   cheaply: a Ghidra loader that imports `dtran`'s recovered structure over a SLEIGH-decoded
   image is the high-value version. An r2 `.r2` script (flags for labels, `Cd`/`Cc` for
   data/comments) is the low-effort version, but without a BESM-6 r2 arch plugin it is just
   your text as annotations over a raw image — limited.

## 6. IDA Pro

IDA is a strong fit in some ways the others are not — but it has one decisive limitation.

**Where IDA fits better than r2/Ghidra:**

- **The processor-module API is imperative C/Python, not a declarative spec.** An IDA
  processor module is a set of callbacks (`ana` decode, `emu` xref/flow, `out` formatting) —
  almost exactly the shape of `dtran`'s `prinsn` / `mklabels` / `find_code_offset` already, so
  you would port your hand-written decoder more or less directly. Ghidra instead forces you to
  re-express decoding as SLEIGH bit-patterns + pcode, which is a real conceptual rewrite. IDA
  is therefore the *least* re-architecting of your existing code.
- **IDA has first-class >8-bit addressable units.** `cnbits` / `dnbits` let a "byte" be wider
  than 8 bits (the DSP modules use 16). This is the one place IDA genuinely beats both r2
  (hard byte model) and Ghidra (configurable but awkward) for a word machine. If you set the
  code unit to **24 bits**, each instruction is exactly one addressable unit (size 1), a
  48-bit word is two units, and data literals are 2-unit items — the cleanest expression of
  your left/right half-instruction model, and it matches how `prsymtab` already walks halves.
  (Whether IDA is fully happy at *48*-bit units specifically would need verifying; 16/24-bit
  is well established.)
- Mature interactive analysis, IDAPython to stamp all your recovered symbols/literals, and a
  signature mechanism (FLIRT/pattern) that is a natural home for your `P/` save/restore stub
  matching.

**The decisive drawback:** **you cannot extend the Hex-Rays decompiler to a new
architecture.** Hex-Rays supports a fixed set of CPUs (x86/64, ARM, PPC, MIPS, RISC-V…);
third parties cannot write a microcode lifter for a custom processor module. So IDA would give
you an excellent *interactive disassembler* for BESM-6 — but **no decompiler**, and your whole
`-d` pipeline is decompiler-centric. Ghidra is the only one of the three whose decompiler is
architecture-agnostic (anything you lift to pcode decompiles). IDA is also commercial/closed
(the decompiler is a separate paid add-on, and custom processor modules need an SDK-capable
license), which matters more for a sharable historical-preservation project than for everyday
x86 work.

## 7. Three-way summary, keyed to the goal

| Goal | Best choice | Why |
|---|---|---|
| Replace the bespoke decompiler | **Ghidra** | Only one with an extensible (pcode) decompiler |
| Best interactive disasm, least port effort, cleanest word model | **IDA** | Imperative module ≈ your `prinsn`; 24-bit `cnbits`; but keep your own decompiler |
| Free / scriptable annotation layer | Radare2 | Weakest fit, byte-addressed, no decompiler |

If your bespoke decompiler is good and Pascal-Autocode-aware, IDA is arguably the most
*pleasant* target — you would reuse `dtran` almost wholesale as an IDA processor + loader
module and feed Hex-Rays-independent output to your existing decompiler, gaining
navigation/xrefs/types for free. If the point is to *retire* the bespoke decompiler, IDA
cannot do it and Ghidra is the answer (with the caveat that Ghidra's generic decompiler may
regress on Pascal idioms until taught them).

## 8. What a web search turned up

- **No existing module anywhere.** Nothing for BESM-6 in Ghidra, IDA, or Radare2 — not in the
  `besm6` organization, not in the r2/Ghidra/IDA plugin ecosystems, not in Russian-language
  results. You would be the first; all hits were generic "how to write a processor module"
  tutorials.
- **The ecosystem already centers on the custom pipeline, and it is a recurring need.** The
  `besm6` organization has at least three reverse-engineering projects — `pascal-re` (the
  Pascal-Monitor compiler), `kalah-re` (the KALAH game — note the `pascal-a.o` sample's name
  word is `КАЛАХ`), and `bega-re` (the "JINN" system) — all running on the same `dtran` (C++)
  → `decomp.pl` (Perl) chain. So this is not a one-off; the same toolchain is pointed at new
  targets repeatedly, which argues *for* investing in something reusable.
- **An authoritative semantics reference already exists, which de-risks the hardest part.**
  The organization has both a working SIMH C simulator (`besm6/simh`, `dispak`, `dubna`) and a
  cycle-level Verilog core (`mesm6`). The hardest part of the Ghidra route — writing correct
  pcode semantics for a 48-bit accumulator/float machine — is normally where such projects
  die, but here the instruction behavior is already pinned down and executable, so a SLEIGH
  lifter can be validated **instruction-by-instruction against the simulator**. That
  meaningfully lowers the Ghidra risk.

**Net, with the search findings:**

- The recurring-target pattern + being first + the existence of a golden simulator tilts the
  calculus toward "a real module is worth it" *if* the goal is interactive analysis or
  retiring `decomp.pl`.
- But the crucial caveat sharpens: your moat is not the disassembler — it is the
  **Pascal-Monitor-specific knowledge baked into `decomp.pl`** (and into `dtran`'s `P/`
  runtime names, stub patterns, symtab walk). All your targets are compiled by the *same*
  Pascal-Monitor compiler, so that shared knowledge is most of the value, and no generic
  Ghidra/Hex-Rays decompiler reproduces it for free.

## 9. Concrete suggestion

Keep `dtran` + `decomp.pl` as the Pascal-Monitor-aware brain regardless. If you want
interactive navigation/cross-references across these projects, add **one** front-end as a
*layer*, not a replacement:

- **IDA processor module** if you are keeping `decomp.pl` — the cheapest port of `dtran`'s
  imperative decoder, with the cleanest word model via 24-bit `cnbits`.
- **Ghidra SLEIGH + loader, validated against SIMH** if the actual goal is to eventually
  retire `decomp.pl` and you are willing to teach Ghidra the Pascal idioms.
- **Radare2** remains the weakest fit.

## Sources

- besm6 GitHub organization — <https://github.com/besm6>
- besm6/pascal-re (and its `decomp.pl`) — <https://github.com/besm6/pascal-re>
- BESM-6 — Wikipedia — <https://en.wikipedia.org/wiki/BESM-6>
