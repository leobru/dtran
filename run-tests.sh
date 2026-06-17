#!/bin/sh
#
# Coverage tests for the combined dtran disassembler (DMS backend).
#
# Each tests/<name>.asm is a small MADLEN source. The runner assembles it with
# ./asm.sh (which drives the `dubna` simulator), disassembles the resulting
# object.o with `dtran -F dms`, and compares the output against the golden
# tests/<name>.expected.
#
# Usage:
#   ./run-tests.sh            run all tests, report PASS/FAIL
#   ./run-tests.sh --update   regenerate the .expected golden files
#   ./run-tests.sh name ...   run only the named tests
#
# Requires: g++, and `dubna` on PATH (the BESM-6 / Dubna monitor simulator).

cd "$(dirname "$0")" || exit 2

if ! command -v dubna >/dev/null 2>&1; then
    echo "SKIP: 'dubna' simulator not found on PATH; cannot assemble test sources." >&2
    exit 77
fi

make -s dtran || exit 2

update=0
case "$1" in
    --update) update=1; shift ;;
esac

if [ "$#" -gt 0 ]; then
    srcs=""
    for n in "$@"; do srcs="$srcs tests/$n.asm"; done
else
    srcs=$(echo tests/*.asm)
fi

pass=0; fail=0
for src in $srcs; do
    name=$(basename "$src" .asm)
    exp="tests/$name.expected"

    # Assemble. asm.sh always rm's object.o first, so a failed assembly leaves
    # no object.o and the disassembly step below will visibly fail the diff.
    ./asm.sh "$src" >"/tmp/dtran-asm-$name.log" 2>&1
    # MADLEN prints "... OSHIB. OPERATOROV NNNN" (operator-error count) on the
    # line just before "*TO PERSO". Grab that line's last field, locale-safely.
    errs=$(awk '/\*TO PERSO/{n=split(prev,a," "); print a[n]} {prev=$0}' asm.lst 2>/dev/null)
    if [ "$(printf '%s' "$errs" | tr -d 0)" != "" ]; then
        echo "FAIL $name (assembler reported $errs operator error(s); listing in asm.lst)"
        fail=$((fail+1))
        continue
    fi

    got="/tmp/dtran-got-$name.txt"
    ./dtran -F dms object.o >"$got" 2>/dev/null

    if [ "$update" -eq 1 ]; then
        cp "$got" "$exp"
        echo "updated $exp"
        continue
    fi

    if [ -f "$exp" ] && diff -q "$exp" "$got" >/dev/null 2>&1; then
        echo "PASS $name"
        pass=$((pass+1))
    else
        echo "FAIL $name"
        diff "$exp" "$got" 2>&1 | sed 's/^/    /'
        fail=$((fail+1))
    fi
done

[ "$update" -eq 1 ] && exit 0
echo "----"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
