#!/usr/bin/env bash
# Closeness report for the Python decompiler rewrite.
#
# The rewrite is a re-architecture (token pipeline), so its output is NOT
# required to byte-match the Perl forks -- the goldens are a bring-up reference.
# This script reports, per (profile, sample), how many lines differ from the
# matching Perl variant's golden, plus the lossless round-trip self-test.
#
#   ./check.sh                 # report all (profile,sample) pairs
#   ./check.sh --update        # regenerate goldens from the Perl variants
set -u
cd "$(dirname "$0")"

# The bring-up reference is the live A/F/G generation of the Perl forks (see
# README): decompF (DMS) supersedes decomp1, decompA (Pascal-Monitor) supersedes
# decomp4, decompG (Pascal-Autocode) supersedes decomp2/3.  Each maps to the
# Python profile that targets the same dialect.
declare -A REF=( [1]=decompF [3]=decompG [4]=decompA )

if [ "${1:-}" = "--update" ]; then
    for v in "${!REF[@]}"; do for f in pa pb dms; do
        perl "../${REF[$v]}.pl" "input/$f.txt" 2>/dev/null > "golden/$f.${REF[$v]}.out"
    done; done
    echo "goldens regenerated from ${REF[*]}"; exit 0
fi

echo "== round-trip (must be lossless) =="
for f in pa pb dms; do
    if diff -q input/$f.txt <(python3 besm6dec.py --roundtrip input/$f.txt) >/dev/null; then
        echo "  OK   $f"
    else
        echo "  FAIL $f"
    fi
done

echo "== closeness to Perl reference (lower diff = closer) =="
printf "  %-9s %-5s %8s %8s %s\n" reference sample diff golden similar
for v in 4 3 1; do for f in pb pa dms; do
    ref=${REF[$v]}
    python3 besm6dec.py --profile $v "input/$f.txt" 2>/dev/null > "/tmp/dec.$v.$f" || { echo "  CRASH $v $f"; continue; }
    grep -v '^Got [0-9]* lines' "golden/$f.$ref.out" > "/tmp/decg.$v.$f"
    gl=$(wc -l < "/tmp/decg.$v.$f")
    d=$(diff "/tmp/decg.$v.$f" "/tmp/dec.$v.$f" | grep -cE '^[<>]')
    pct=$(( gl == 0 ? 0 : 100 * (2*gl - d) / (2*gl) ))
    printf "  %-9s %-5s %8s %8s ~%s%%\n" "$ref" "$f" "$d" "$gl" "$pct"
done; done
