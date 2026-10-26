#!/bin/bash
# Runs every renderer against every dataset, one fresh browser each, into
# results/matrix.jsonl (tabulate with: node table.mjs results/matrix.jsonl).
# Needs `npm run serve` running, `npm run data`, and `npm run build-sigma`.
# django-* datasets are optional real exports (see fromapi.mjs).
cd "$(dirname "$0")"
mkdir -p results shots
out=results/matrix.jsonl
run() { echo "$(date +%T) load=$(cut -d' ' -f1 /proc/loadavg) $1 $2" >> results/matrix.log; timeout 900 node bench.mjs $1 $2 $out >> results/matrix.log 2>&1 || echo "{\"renderer\":\"$1\",\"ds\":\"$2\",\"fatal\":\"timeout/exit\"}" >> $out; }
for ds in synth2k django-capped synth10k django-uncapped synth50k; do
  [ -f data/$ds.compact.json ] || continue
  for r in current webgl cyweb sigma cosmos d3canvas forcegraph; do
    if [ "$ds" = synth50k ] && [ "$r" = forcegraph ]; then continue; fi
    run $r $ds
  done
done
echo done >> results/matrix.log
