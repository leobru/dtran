# besm6dec - Python rewrite of the `decomp*.pl` decompiler

`besm6dec.py` is a re-architected, single-tool replacement for the Perl
decompiler forks (the live generation `../decompA … decompG.pl`; the earlier
`decomp1 … decomp4` snapshots they grew from are no longer kept in the repo).
It is the **second stage** of the BESM-6 pipeline: `dtran -d` emits a bespoke
pseudo-assembly listing, and this tool lifts it into Pascal-like pseudocode.

## Why a rewrite

The Perl forks are four ~800-line variants that are 70–80 % identical; they
differ along two axes only:

* **Target** — three of them, over two calling conventions: **DMS** (decomp1,
  `*nnnn:` labels) and **Pascal-Monitor** (decomp4, `Lnnnn:`) both call via reg
  13 / runtime via 14; **Pascal-Autocode** (decomp2/3, `Lnnnn:`) calls via reg
  14 / runtime via 12;
* **Optional features** — register tracking + `knargs` (Pascal-Autocode), real
  Pascal declarations (decomp4), array globals / real-compares (decomp3), etc.

Each works by slurping the whole listing into one `;`-joined megastring and
applying ~150 ordered `s///g` substitutions. That string model is exactly what
makes them unmaintainable. The rewrite expresses the same transformations as a
**token pipeline** and the family/feature differences as **data** (a `Dialect`),
so there is one engine instead of four forks.

## Architecture

```
nodes = tokenize(text)            # list[Node]: Insn | Label | Header | Text | Comment | Raw
for p in PIPELINE:                # each pass: (Dialect, list[Node]) -> list[Node]
    nodes = p(dialect, nodes)
print(render_out(nodes))          # ;-terminated surface form
```

* **Nodes** — `Insn(reg, op, arg, label)` for decoded instructions/data,
  `Label`/`Header` for structure, `Text` for emitted statements, `Comment` for
  flush-left banners, `Raw` for anything that didn't fit the grammar.
* **`Dialect`** — `call_reg`, `runtime_reg`, `label_style`, `global_prefix`,
  and the feature toggles `track_regs` / `known_args` / `emit_pascal_decls` /
  `underscore_kw`.  Three profiles, one per target: `1` = DMS, `3` =
  Pascal-Autocode, `4` = Pascal-Monitor.  The bring-up reference is the **live
  A/F/G generation** of the Perl forks: `decompF` (DMS) supersedes `decomp1`,
  `decompG` (Pascal-Autocode) supersedes `decomp2/3`, `decompA` (Pascal-Monitor)
  supersedes `decomp4`.  (decomp2 was already subsumed by decomp3, which decompG
  in turn supersedes.)
* **`PIPELINE`** — 36 ordered passes in five slices: front-end normalization →
  prologue recognition + `processprocs` → pre-stack recognizers (constants,
  indirect addressing, calls, casts, branch folding) → the **stack machine** →
  back-end substitution (sets, relops, struct fields, FUNCRET, Pascal decls).

The stack machine (`stack_machine`) is a faithful port of decomp4's accumulator
interpreter; decomp2/3's register tracking and `knargs` arity handling are
enabled by the dialect.

## Usage

```
dtran -d -F dms file.o | python3 besm6dec.py --profile 1    # DMS
dtran -d -F pa  file.o | python3 besm6dec.py --profile 3    # Pascal-Autocode (KALAH-style)
dtran -d -F pb  file.o | python3 besm6dec.py --profile 4    # Pascal-Monitor
python3 besm6dec.py --profile 4 listing.txt                 # from a file
python3 besm6dec.py --roundtrip listing.txt                 # lossless spine self-test
```

## Validation

The Perl variants disagree with each other, so the rewrite is **not** required
to byte-match any one of them; the Perl outputs are a bring-up reference.
`check.sh` reports, per (profile, sample):

* a **lossless round-trip** (tokenize → render reproduces the raw listing
  byte-for-byte — currently 3/3 inputs);
* **closeness** to the matching live Perl variant's golden (diff-line count).

Current closeness, each profile against its own intended target sample:

| profile | target | reference | sample | closeness |
|---------|--------|-----------|--------|-----------|
| profile 4 | Pascal-Monitor  | `decompA` | `pb` | ~95% |
| profile 1 | DMS             | `decompF` | `dms`| ~95% |
| profile 3 | Pascal-Autocode | `decompG` | `pa` | ~78% |

Profile 3 dropped from ~95% (vs the older decomp3) to ~78% when re-based onto the
much richer `decompG`, then climbed back as decompG features were ported
(`_while`, real `_proced`/`_function`/`_var` declarations, the `g` global
prefix).  The bulk of the remaining gap is decompG's later folds not yet ported
(see Known gaps).

The **Pascal-Autocode target uses the underscore-keyword style** (`_if`/`_then`/
`_else`, and a leftover `,ATX,X` rendered as the empty-RHS `X := `), per decompG,
the canonical reference for this target.

(`pb`/`pa` are Pascal-exec; `dms` is the DMS container. Each Perl variant was
written for one of these, so cross-pairing a profile with a non-matching sample
is expected to diverge.)

## Known gaps (deliberate or pending)

* **Not replicated (Perl bugs):** the `1RETURN` artifact from a substring-match
  on `13,UJ,0`; the trailing `;` glued onto the last `C ----` comment before
  indented code. The rewrite emits the cleaner form.
* **Pending decompG folds (the bulk of profile 3's remaining gap):** the
  `R13 := &X; … := pck/unpck/get/put` argument folds (~100 lines), the
  `@.f[0]` → `@` strip (~66 lines), the `output@ := X; put(output)` →
  `write(X)` and `writeInt/writeCharWide` → `write(x:w)` folds (~55 lines), and
  the `_or` / single-statement structured-`if` recognition.  Also pending: the
  `symbol/operator/options/form` enum-file substitution and decompF's DMS
  `getString` write-literal extraction.  These are config-driven / local
  rewrites and slot in as additional passes.
* The remaining stack-machine diff is fine-grained grouping (~100 `#…` dumps).
  The Pascal-Autocode stack machine resets `stack`/`regs` at every label
  (decompG's `@stack = () if /:,/`), discarding a basic block's leftover stack
  as dead rather than dumping a premature `#…`; the 1 still-missing of 10
  `_while` and a few `_for` are blocked by a deeper register-field divergence
  (`4,XTA,5` not lifted to `R4->5`), not by dumps.

Ported so far: the for-loop stack-transform, register faking, the pre-seeded
constant tables, `setup`/`rollup`, the DMS `P/1D` static-init mapping, the DMS
and Pascal-Autocode I/O runtime calls
(`writeString/Int/LN/Char/CharWide/Alfa`, `eof/eoln/get/put/reset/rewrite`),
`new`, the set-membership `IN` → `ifgoto`/`ifnot` fold, the non-local `GOTO`
via `P/RC`, and the Pascal-Autocode string/char write operators
(`write('…')`/`BIND` via the GOST data section, `writeInt`/`writeCharWide`, and
the width forms `write('…':w)` / `write(file,'…':w)` via `P/A7`/`P/0071`), and
the Pascal-Autocode underscore-keyword style (`_if`/`_then`/`_else`, empty-RHS
`X := `), and the Pascal-Autocode cleanups (drop word-alignment ` :,BSS,` and
the `11,MTJ,d` frame-restore that blocks return recognition), and the
Pascal-Autocode `,ITS,11` register-save prologue pre-transform (→ standard
many-args form for prologue recognition), `_for`-loop recognition
(nesting-aware, tolerant of loops sharing an exit address), `_while`-loop
recognition (decompG: a top-tested `_if COND _then goto exit … ,UJ,top` →
`_while _not COND _do _(…_)`), and the Pascal-Autocode declarations (decompG
`mkargs`/`mkvars`: an unambiguous header → `(* Level n *) _proced
NAME(args:integer); _var …:integer; _(`, `_function …:integer`, locals > 100
spilled to `_array [101..n] _of integer`, runtime `RETURN` → `EXIT`/`_)`).
* **Per-target tables** (`routines`/`globals`/`locals`/`symbol`/…) are not yet
  loaded; `Dialect.const_map` is the hook for them.
