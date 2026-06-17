# Combined `dtran` — design

A single BESM-6 disassembler that subsumes the five `dtran*.cc` sources, supporting
all three input formats with the union of their features, selected by auto-detection
(overridable with `-F`).

## 1. Source lineage (what we are merging)

| File | Family | On-disk format | Role in merge |
|------|--------|----------------|---------------|
| `dtran2.cc` | DMS object | sectioned; packed **and** unpacked header; magic key + entry table | **primary DMS reader** |
| `dtran3.cc` | DMS object | sectioned; packed header only | DMS BSS-tolerance + OOM check + `prbss` note |
| `dtran1.cc` | Pascal exec **A** | load base 0; `total_len=memory[5]`, entry `memory[010]`; GOST | **primary Pascal-A reader** (correct reachability) |
| `dtran5.cc` | Pascal exec **A** | same as dtran1 | adds `-G`/`gostoff`, `-f`/`forced_code_off`, jump tables, scored GOST |
| `dtran4.cc` | Pascal exec **B** | load base `02000`; paged `total_len`; GOST+ITM+ISO+TEXT | **primary Pascal-B reader** (richest) |

Confirmed by the author: Pascal-A and Pascal-B are genuinely **two distinct formats**
(different load base and header layout), so the combined tool keeps three backends.

## 2. Format model & detection

```
enum Format { FMT_DMS, FMT_PASCAL_A, FMT_PASCAL_B };
```

Detection runs on a raw word buffer `raw[n]` (`n = filesize/6`) **before** choosing a
load base, then the loader relocates `raw` into `memory[]` at the format's base:

- **DMS** — `raw[0] == BESM6_MAGIC` (0x4245534d3600), or a packed/unpacked header whose
  computed `comment_off` fits the file. Load base 0 (skip magic word if present),
  unpacked-header detection via `(raw[1] >> 45) != 0` and entry-table skip (from dtran2).
- **Pascal-A** — `raw[5]` plausible as `total_len` (`0 < t < 32768`, `t <= n`) and
  `raw[010]` an entry inside `[0,t)`. Load base 0.
- **Pascal-B** — paged length from `raw[8]/raw[9]` (i.e. `memory[02010]/[02011]` once
  loaded at `02000`): `total_len = ((raw[9] && raw[9]<037) ? raw[9] : raw[8]) * 02000 + 02000`,
  plausible against `n`. Load base `02000` (`memory[02000+i] = raw[i]`).

`-F dms|pa|pb` forces the choice and skips sniffing. Detection order: DMS → Pascal-A →
Pascal-B; ambiguity falls through to a diagnostic asking for `-F`.

## 3. Architecture

One `struct Dtran` carrying `Format fmt` plus a set of feature flags **derived** from it,
so the bulk of the machinery is shared and only genuinely-divergent steps branch:

| Flag | DMS | Pascal-A | Pascal-B |
|------|-----|----------|----------|
| `sectioned` (code/const/data by header lengths) | ✓ | – | – |
| `reachability` (`find_code_offset` + `code_map`) | – | ✓ | ✓ |
| `load_base` | 0 | 0 | 02000 |
| `file_symtab` (`dump_symtab`) | ✓ | – | – |
| `pascal_symtab` (`prsymtab`) | – | – | ✓ |
| `data_section` (`prdata`/`prsets`) | ✓ | – | – |
| `ext_calls_high` (`≥074000` → `P//C/`) | – | ✓ | – |
| `basereg_is_base` (base arithmetic + `BASE`) | ✓ | – | ✓ |
| `multi_encoding` | ISO,TEXT | GOST(+`-G`) | GOST,ITM,ISO,TEXT,auto |
| `label_patterns` | – | p2/3/4/32/43 + hard table | save/restore family + EF/E/1D |

**Shared vs. forked.** Opcode decode is shared (`get_opidx` + opname/reg/arg extraction);
the parts that differ by family — operand resolution, label creation, the section/print
drivers, literal classification, and symbol handling — are split into `_dms` / `_pascal`
(or `_pa`/`_pb`) helpers selected by `fmt`. This keeps a single instruction decoder while
being honest about where the families actually diverge.

```
Dtran::run()
 ├─ FMT_DMS      → dump_symtab(); prtext_dms(); prconst(const);
 │                 if data: ",DATA," + (litconst ? prdata() : prconst()+prsets())
 ├─ FMT_PASCAL_A → prconst_pascal(); prtext_pascal();           // ext_calls_high path
 └─ FMT_PASCAL_B → prtext_pascal(); prsymtab();                 // multi_encoding path
```

## 4. Function-by-function merge map

Each combined function and where it comes from / what changes:

### Globals
- **`op[]` opcode table** — identical across all five. Canonical choices: name `0x025000`
  as **`J+M`** (dtran2's spelling; the MADLEN assembler accepts only `J+M` — verified by the
  coverage tests in §8 — so the earlier `M+J` normalization was wrong). `YTA` = **`OPCODE_IMM64`** (per author),
  and the `64±N` rule applies to the **whole IMM64 family** (`YTA`/`E+N`/`E-N`/`ASN`):
  - zero immediate → bare op, no operand (never `64-64`);
  - **every** non-zero immediate → `64%+d` (`val-64`), including values `< 8` and after `UTC`/`WTC`.

  IMM64 is checked *before* the shared `val < 8` / `prev_addrmod` shortcut, while the outer
  `if (uint val = struc ? arg2 : arg1)` zero-guard still skips a zero arg:

  ```c
  } else if (uint val = struc ? arg2 : arg1) {      // val != 0
      if (type == OPCODE_IMM64)                     // YTA/E+N/E-N/ASN: always 64±N
          operand = strprintf("64%+d", val - 64);
      else if (type == OPCODE_REG1 || val < 8 || prev_addrmod)
          operand = strprintf("%d", val);
      else if (!struc && !reg && type != OPCODE_IMMEX && /*const range*/)
          operand = labels[arg1];
      else
          operand = strprintf(nooctal && type != OPCODE_IMMEX ? "%d" : "%oB", val);
  }
  ```
- **`strprintf`** — from dtran3 (`vasprintf < 0 → exit`) / dtran2 (`perror`); keep the
  error check (dtran1/4/5 omit it).
- **`gost_to_utf`** — from dtran1/5, with `[026] = ";"` (drop dtran4's debug `"smc"`).
- **`text_to_utf`**, **`gost_to_itm`**, **`itm_to_utf`**, **`populate_itm()`** — from dtran4.
- **`BESM6_MAGIC`** — from dtran2.
- **offset sets** `gostoff/itmoff/isooff/textoff`, `forced_code_off` — from dtran4 (dtran5 has only `gostoff`).

### Loader / header
- **`freadw`** — identical everywhere.
- **`load_file()`** (new) — read into `raw[]`, run `detect_format()` (§2), relocate to
  `memory[load_base + i]`, skip DMS magic, length-sanity check (union of the five checks),
  trailing-zero trim for Pascal-B (`while (memory[total_len-1]==0) --total_len;`, from dtran4).
- **`fill_lengths_dms()`** — from **dtran2** (packed+unpacked, `head_off` scan, `cmd_off`
  indirection, full `C ...` banner). BSS handling relaxed to **warn** (dtran3) instead of
  hard `exit` (dtran2).
- **`fill_lengths_pa()`** — from dtran1/5 (`total_len=memory[5]`, `main_off=memory[010]`,
  `code_off = find_code_offset()`).
- **`fill_lengths_pb()`** — from dtran4 (paged `total_len`, `main_off=02000`,
  `code_off = max(main_off, find_code_offset())`). Pascal-B has no name/date word in the file
  (dtran4 read the unloaded `memory[1]/[2]`), so emit a **pro-forma `PASCODER:,NAME, NEW DTRAN`**
  line and **no** compilation-date line. The `C Memory size / Code start / Program start` info
  comments stay (derived from computed values).

### Reachability (Pascal A + B)
- **`mklabel`** — from dtran1/4 (`void`, `labels[off]="L%04o"`); fixes dtran5's non-returning `uint`.
- **`check_chain`** — from dtran4 (IN-type `13,VTM / 14,VJM` chain). Pascal-B; safe to enable for A.
- **`find_code_offset`** — **dtran4's correct decode** (`opcode=(cinsn & 03700000)>>15`),
  superseding **dtran5's broken `(cinsn & 037)`**. Includes VJM/VZM/V1M call+fallthrough,
  UJ + jump-table heuristic, U1A/UZA incl. `8,UZA` case, `check_chain`, `forced_code_off`.
  This is the union of dtran1's correct masks + dtran4/5's extensions.
- **`label_pattern` / `label_patterns`** — merge dtran4 (register save/restore `P/MN`,
  `P/EF`, `P/E`, `P/1D`, `code_len`) with dtran1/5's `p2/p3/p4/p32/p43`. Re-enable the
  hard-coded `symtab[...] = "P/..."` table (active in dtran1/5, `#if 0` in dtran4) as a
  Pascal-A default; **open** whether to apply it to Pascal-B.

### Decode & print
- **`get_opidx`** — from dtran4 (`do/while`, tests the catch-all). Fixes dtran1/5's
  fall-off-end (no `return`).
- **`prinsn`** — shared head (opname, illegal-opcode formatting, reg, arg1/arg2/struc,
  `prev_addrmod`, the `nolabels` `L`/`*`-rewrite tail) + **`resolve_operand_{dms,pa,pb}()`**:
  - `_dms` (dtran2/3): `arg2≥074000`→symtab; `arg2≥040000`→`labels`; `!struc 04000..010000`→symtab; `≥070000`→`"dunno"`; basereg→`BASE`/`labels[baseaddr+arg1]`.
  - `_pa` (dtran1/5): `arg2≥074000`→symtab w/ `P/` fallback; `≥074000 !struc`→`C/`; `code_off` immediate check.
  - `_pb` (dtran4): `labels[off]` with `code_off ≤ off ≤ total_len` guard; basereg base arithmetic (`arg1 += baseaddr`, `BASE`).
- **`mklabels`** — split `mklabels_dms` (dtran2/3, base arithmetic) / `mklabels_pa`
  (dtran1/5) / `mklabels_pb` (dtran4, `op∈{030,037,024}`, reg 10/11 special cases).
- **`get_literal`** — `_dms` (dtran2/3, `quoteiso`/ISO/TEXT, `cmd_off`/`+3`) /
  `_pascal` (dtran4 `format_map`-driven, superset of dtran1/5's `is_likely_gost`).
- **`populate_formats`** — from dtran4 (Pascal `format_map` heuristic); used by both A and B
  (A simply resolves to GOST/INT/LOG most of the time).
- **`pr1const`** — `_dms` (dtran2/3) / `_pascal` (dtran4, `format_map` + offset sets).
- **`prconst`** — `_dms(addr,len,litconst)` (dtran2/3) / `_pascal(litconst)` over `[011,code_off)` (dtran1/5/4).
- **`prtext`** — `_dms` (cmd section via `cmd_off`, dtran2/3) / `_pascal` (`code_off..total_len`
  with `code_map`, inline const, page markers, dtran4 superset of dtran1/5).
- **`prbss`** — from dtran3 (warning note).

### Symbol tables
- **DMS:** `gak`, `dump_sym`, `dump_symtab` — from dtran2/3 (identical; use `nooctal` form).
- **Pascal-B:** `opfields`, `decode`, `prsyms`, `prsymtab` — from dtran4 (verbatim).

### Encoders / detectors (shared library)
- `get_utf8`, `get_gost_char/word`, `get_bytes` — from dtran1/4/5.
- `get_itm_char/word`, `get_text_char/word` — from dtran4.
- `get_iso_char/word` — dtran4's richer version (`_%03o` for out-of-range).
- `is_likely_iso`/`is_valid_iso`, `is_likely_text`, `is_likely_gost`/`is_valid_gost`,
  `is_likely_itm`/`is_valid_itm` — from dtran4 (scored), which subsumes dtran1's simple
  `is_likely_gost` and dtran2/3's `is_likely_iso/text`. Keep the optional `gostoff`
  short-circuit (dtran5's `is_likely_gost(addr,val)`).

### `main`
- Union getopt: **`cdelnoeR:E:G:I:A:T:f:F:`**.
  - `-l` nolabels, `-o` nooctal, `-c` litconst, `-R` basereg, `-d` all-in-one (per-format defaults),
  - `-e` noequs (DMS), `-n` nodlabels (Pascal), `-E` entry-points,
  - `-G/-I/-A/-T` GOST/ITM/ISO/TEXT offset files (Pascal), `-f` forced code offset, `-F` format override.
- Initialize **all** `FILE*` to `NULL` (fix dtran5's uninitialized `gost`).
- Dispatch by detected/forced `fmt` to `Dtran::run()`.

## 5. Bug fixes folded into the merge
1. `get_opidx` no-return (dtran1/5) → `do/while` (dtran4).
2. `find_code_offset` `(cinsn & 037)` (dtran5) → `(cinsn & 03700000)`.
3. Uninitialized `FILE* gost` (dtran5) → `= NULL`.
4. `mklabel` declared `uint` but returns nothing (dtran5) → `void`.
5. `gost_to_utf[026]` `"smc"` debug placeholder (dtran4) → `";"`.
6. Opcode `0x025000` mnemonic kept as **`J+M`** (dtran2). The earlier merge wrongly
   normalized it to `M+J`; MADLEN accepts only `J+M` ("error in opcode" otherwise), so the
   combined tool must emit `J+M` for a re-assemblable `-e -l` round-trip (see §8).
7. Dead `prsets` `strtol` block (dtran2/3) → removed.
8. Pascal-B banner reading unloaded `memory[1]/[2]` (dtran4) → banner dropped entirely (§6).

## 6. Resolved decisions (author, 2026-06-16)
- **IMM64 family** (`YTA`/`E+N`/`E-N`/`ASN`) = `OPCODE_IMM64`; zero immediate → bare op, every
  non-zero immediate → `64±N`, checked before the `val < 8`/`prev_addrmod` shortcut. See §4.
- **Pascal-B name line** — emit a pro-forma ` PASCODER:,NAME, NEW DTRAN`; no compilation-date line.
- **Jump-table / `check_chain` / `8,UZA` heuristics** — enabled for **both** Pascal-A and
  Pascal-B (single unified `find_code_offset`).
- **Hard-coded `P/` runtime symtab** — **Pascal-A only**. Pascal-B relies solely on
  `label_patterns` (its `P/` table stays `#if 0`).

## 7. Build & validation  — DONE (2026-06-16)
- `Makefile` added (`make dtran`; `make legacy` builds dtran1-4 for comparison).
- `dtran.cc` compiles **`-Wall -Wextra` clean, zero warnings**.
- Auto-detection verified on all three samples (`dms.o`→DMS, `pascal-a.o`→Pascal-A,
  `pascal-b.o`→Pascal-B); `-F dms|pa|pb` produces byte-identical output to auto-detect.
- Golden diffs (both default and `-d`) vs the originals:
  - **DMS vs dtran2** — identical except the intended IMM64 fix (16 `,YTA,64`→`,YTA,64+0`). No other diffs.
  - **Pascal-B vs dtran4** — identical except: name line `000000:`→`PASCODER:`, dropped
    compilation-date line, 2 `,ASN,80`→`,ASN,64+16` (IMM64 generalization), and 5 `,M+J,`→`,J+M,`
    (mnemonic correction, §8).
  - **Pascal-A vs dtran1** — only (a) the IMM64 rendering fix (`YTA`, and `ASN` after `UTC`/`WTC`)
    and (b) 3 regions that dtran1 dumped as `,LOG`/`,GOST` data and the enhanced reachability
    now decodes as well-formed code (jump-table / `8,UZA` following). Verified: **no** dtran1-side
    changed line is anything other than an IMM64 op or a data line → no regressions, purely additive.
- dtran5 is intentionally not a reference: it does not compile (`uint64_t` without `<cstdint>`)
  and its reachability decode `(cinsn & 037) >> 15` is always 0.

## 8. Coverage tests (DMS round-trip)

`tests/*.asm` are small MADLEN sources, each exercising one area. `run-tests.sh` assembles
each with `./asm.sh` (driving the `dubna` simulator), disassembles the resulting `object.o`
with `dtran -F dms`, and diffs against the golden `tests/<name>.expected`:

| test | covers |
|------|--------|
| `imm64`   | `YTA`/`ASN`/`E+N`/`E-N` — zero→bare, every non-zero→`64±N` (incl. `<8` and `=64`) |
| `str1`    | all short-address ops `ATX…XTR` (incl. `A/X`, `A*X`) |
| `regops`  | `ATI/STI/ITA/ITS/MTJ/J+M`, `VTM/UTM`, `RTE/NTR` |
| `control` | `UJ`, `UZA/U1A/VZM/V1M/VLM`, `VJM`, `UTC/WTC` with a label target |
| `extops`  | extended `*50/*51/*60/*64/*70/CTX/*76` and `*74` stop |
| `literals`| constant pool: `INT` (small/large), `LOG`, `TEXT` (8H), `ISO` (6H) |

```
./run-tests.sh            # assemble + diff all, report PASS/FAIL
./run-tests.sh --update   # regenerate the .expected goldens
./run-tests.sh imm64 str1 # run named tests only
```

Requires `dubna` on PATH (skips with exit 77 otherwise). `object.o` is byte-deterministic
across assembler runs, so golden diffs are stable. **Finding:** these tests proved MADLEN
accepts only `J+M` for opcode `0x025000`, fixing the earlier `M+J` regression (§5.6).

## 9. Open follow-up
- BSS sections (DMS) and the `-I`/`-A`/`-T` Pascal-B offset paths are carried over but not
  exercised by the current samples.
- Auto-detect requires `cmd_len > 0`, so a command-less DMS module (e.g. an empty or
  const/data-only object) is not detected and needs explicit `-F dms`. The coverage tests
  force `-F dms` for this reason.

## 10. Result
`dtran.cc` is ~1450 lines (shared core, three backends, `main`). Build with `make`.
Coverage: `./run-tests.sh` (6 DMS round-trip tests).
