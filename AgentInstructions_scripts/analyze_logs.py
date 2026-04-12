#!/usr/bin/env python3
"""
analyze_logs.py — Universal AutoCooker logs.json analyzer.

Usage:
    python analyze_logs.py <path/to/logs.json>           # full report
    python analyze_logs.py <path/to/logs.json> --filter RECONSTRUCT
    python analyze_logs.py <path/to/logs.json> --section errors
    python analyze_logs.py <path/to/logs.json> --section timeline
    python analyze_logs.py <path/to/logs.json> --section validation
    python analyze_logs.py <path/to/logs.json> --section json_errors
    python analyze_logs.py <path/to/logs.json> --section summary
    python analyze_logs.py <path/to/logs.json> --unique-errors

Sections: timeline, errors, validation, json_errors, reconstructs, summary (default: all)
"""

import json
import sys
import argparse
from collections import Counter

# Fix UnicodeEncodeError on Windows (cp1251 terminal)
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_logs(path: str) -> list:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_all(logs: list) -> dict:
    data = {
        "json_errors": [],
        "validation_errors": [],
        "reconstructs": [],
        "phase_headers": [],
        "warnings": [],
        "all_errors": [],
    }

    for i, entry in enumerate(logs):
        if not isinstance(entry, dict):
            continue
        ts = entry.get("ts", "?")
        phase = entry.get("phase", "")
        msg_type = entry.get("type", "")
        msg = entry.get("msg", "")

        if "[JSON INVALID]" in msg:
            data["json_errors"].append({"index": i, "ts": ts, "msg": msg})

        if "[RETRY]" in msg and "Validation failed" in msg:
            data["validation_errors"].append({"index": i, "ts": ts, "msg": msg[:1000]})

        if "[RECONSTRUCT]" in msg:
            data["reconstructs"].append({"index": i, "ts": ts, "msg": msg})

        if msg_type == "phase_header" or "═══" in msg:
            data["phase_headers"].append({"index": i, "ts": ts, "phase": phase, "msg": msg})

        if msg_type in ("warn", "error") or any(
            kw in msg for kw in ("[ERROR]", "[FAIL]", "Traceback", "Exception")
        ):
            data["warnings"].append({"index": i, "ts": ts, "type": msg_type, "msg": msg[:300]})

    return data


def print_section(title: str, items: list, fmt):
    sep = "=" * 80
    print(f"\n{sep}\n{title}\n{sep}")
    if not items:
        print("  (none)")
        return
    for i, e in enumerate(items, 1):
        fmt(i, e)


def print_summary(logs: list, data: dict):
    sep = "=" * 80
    print(f"\n{sep}\nSUMMARY\n{sep}")
    print(f"  Total log entries : {len(logs)}")
    print(f"  Phase transitions : {len(data['phase_headers'])}")
    print(f"  JSON parse errors : {len(data['json_errors'])}")
    print(f"  Validation errors : {len(data['validation_errors'])}")
    print(f"  RECONSTRUCT triggers: {len(data['reconstructs'])}")
    print(f"  Warnings/errors   : {len(data['warnings'])}")

    if data["validation_errors"]:
        # Extract unique error messages
        msgs = [e["msg"] for e in data["validation_errors"]]
        print("\n  Unique validation error patterns:")
        seen = set()
        for msg in msgs:
            # Take first 120 chars as fingerprint
            key = msg[:120]
            if key not in seen:
                seen.add(key)
                print(f"    • {key[:120]}")


def print_unique_errors(data: dict):
    sep = "=" * 80
    print(f"\n{sep}\nUNIQUE VALIDATION ERROR PATTERNS\n{sep}")
    counter = Counter()
    for e in data["validation_errors"]:
        # Fingerprint: first 150 chars
        counter[e["msg"][:150]] += 1
    for pattern, count in counter.most_common():
        print(f"\n[×{count}] {pattern}")


def main():
    parser = argparse.ArgumentParser(description="Analyze AutoCooker logs.json")
    parser.add_argument("logfile", help="Path to logs.json")
    parser.add_argument(
        "--section",
        choices=["timeline", "errors", "validation", "json_errors", "reconstructs", "summary", "all"],
        default="all",
        help="Which section to print (default: all)",
    )
    parser.add_argument("--filter", metavar="TEXT", help="Only show entries containing TEXT")
    parser.add_argument("--unique-errors", action="store_true", help="Show deduplicated validation error patterns")
    args = parser.parse_args()

    logs = load_logs(args.logfile)

    # Apply filter if requested
    if args.filter:
        kw = args.filter.lower()
        logs = [e for e in logs if kw in str(e).lower()]
        print(f"[filter: '{args.filter}'] → {len(logs)} entries matched\n")

    data = extract_all(logs)

    if args.unique_errors:
        print_unique_errors(data)
        return

    s = args.section

    if s in ("timeline", "all"):
        print_section(
            "PHASE TRANSITIONS",
            data["phase_headers"],
            lambda i, e: print(f"[{e['index']:4d}] {e['ts']} | {e.get('phase',''):10s} | {e['msg'][:80]}"),
        )

    if s in ("json_errors", "all"):
        print_section(
            "JSON PARSE ERRORS",
            data["json_errors"],
            lambda i, e: print(f"\n{i}. [{e['index']:4d}] {e['ts']}\n   {e['msg']}"),
        )

    if s in ("validation", "errors", "all"):
        print_section(
            "VALIDATION ERRORS",
            data["validation_errors"],
            lambda i, e: print(f"\n{i}. [{e['index']:4d}] {e['ts']}\n   {e['msg']}"),
        )

    if s in ("reconstructs", "errors", "all"):
        print_section(
            "RECONSTRUCT TRIGGERS",
            data["reconstructs"],
            lambda i, e: print(f"\n{i}. [{e['index']:4d}] {e['ts']}\n   {e['msg']}"),
        )

    if s in ("summary", "all"):
        print_summary(logs, data)


if __name__ == "__main__":
    main()
