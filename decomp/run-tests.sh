#!/bin/sh
#
# Coverage tests for DMS-format *decompilation* (the second pipeline stage).
#
# Each tests/<name>.pas is a small Dubna-Pascal program; by convention its first
# line is `(*=p-,t-,s8*)program main(output);`. The runner compiles it to a DMS
# object with ./pasdms.sh (driving the `dubna` simulator), disassembles object.o
# with `dtran -d -F dms`, decompiles that listing with `besm6dec.py --profile 1`,
# and compares the result against the golden tests/<name>.expected.
#
# Usage:
#   ./run-tests.sh            run all tests, report PASS/FAIL
#   ./run-tests.sh --update   regenerate the .expected golden files
#   ./run-tests.sh name ...   run only the named tests
#
# Requires: `dubna` on PATH, python3, and a buildable ../dtran (built via make).

cd "$(dirname "$0")" || exit 2

if ! command -v dubna >/dev/null 2>&1; then
    echo "SKIP: 'dubna' simulator not found on PATH; cannot compile test sources." >&2
    exit 77
fi

make -C .. -s dtran || exit 2

update=0
case "$1" in
    --update) update=1; shift ;;
esac

if [ "$#" -gt 0 ]; then
    srcs=""
    for n in "$@"; do srcs="$srcs tests/$n.pas"; done
else
    srcs=$(echo tests/*.pas)
fi

pass=0; fail=0
for src in $srcs; do
    name=$(basename "$src" .pas)
    exp="tests/$name.expected"

    # Compile + link to a DMS object.  pasdms.sh rm's object.o first, so a failed
    # build leaves none and the module-banner check below trips.
    ./pasdms.sh "$src" >"/tmp/dec-pas-$name.log" 2>&1

    got="/tmp/dec-got-$name.txt"
    ../dtran -d -F dms object.o 2>/dev/null \
        | python3 py/besm6dec.py --profile 1 >"$got" 2>/dev/null

    # A good build always opens with the ` MAIN :,NAME,` module banner.
    if ! grep -q ',NAME,' "$got"; then
        echo "FAIL $name (compile/link produced no DMS module; see /tmp/dec-pas-$name.log)"
        fail=$((fail+1))
        continue
    fi

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
