#!/bin/bash

echo "=== CHRONOLOGICAL TIMELINE OF ALL VALIDATION ERRORS ==="
grep -n "\[RETRY\] Validation failed" logs.json | while read line; do
    linenum=$(echo "$line" | cut -d: -f1)
    entry_num=$((($linenum - 2) / 5))
    ts_line=$((linenum - 2))
    
    sed -n "${ts_line}p" logs.json
    sed -n "${linenum}p" logs.json | sed 's/^[^:]*: //' | cut -c1-500
    echo "---"
done

