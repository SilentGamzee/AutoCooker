#!/bin/bash

echo "=== 1. ALL UNIQUE JSON INVALID ERRORS ==="
grep "\[JSON INVALID\]" logs.json | sed 's/.*\[JSON INVALID\]/[JSON INVALID]/' | sort | uniq -c
echo ""

echo "=== 2. ALL UNIQUE RETRY VALIDATION ERRORS (TRUNCATED TO ~300 CHARS) ==="
grep "\[RETRY\]" logs.json | sed 's/.*\[RETRY\] Validation failed:/[RETRY]/' | cut -c1-300 | sort | uniq -c
echo ""

echo "=== 3. ALL RECONSTRUCT EVENTS ==="
grep "\[RECONSTRUCT\]" logs.json
echo ""

echo "=== 4. OTHER TYPE=error ENTRIES ==="
grep '"type": "error"' logs.json | grep -v "JSON INVALID"
