"""Summarise /var/lib/storage-api/default-master-usage.jsonl (AP 2.6, #1960).

Usage: python3 scripts/default_master_report.py [path]
"""
import json
import sys
from collections import Counter, defaultdict

path = sys.argv[1] if len(sys.argv) > 1 else "/var/lib/storage-api/default-master-usage.jsonl"
by_caller = Counter()
priv_by_caller = Counter()
paths = defaultdict(Counter)
first = last = None
with open(path, encoding="utf-8") as fh:
    for line in fh:
        r = json.loads(line)
        caller = (r["peer"], r["xff"] or "-", r["ua"] or "-")
        if r.get("priv"):
            # requests where is_privileged_admin said yes for the default key
            priv_by_caller[caller] += r["n"]
            paths[caller][f'PRIV {r["method"]} {r["path"]}'] += r["n"]
            continue
        by_caller[caller] += r["n"]
        paths[caller][f'{r["method"]} {r["path"]}'] += r["n"]
        first = r["since"] if first is None else min(first, r["since"])
        last = r["until"] if last is None else max(last, r["until"])
print(f"window {first} .. {last}  calls {sum(by_caller.values())}  callers {len(by_caller)}")
for caller, n in by_caller.most_common():
    print(f"{n:7d}  (priv {priv_by_caller[caller]})  peer={caller[0]}  xff={caller[1]}  ua={caller[2]}")
    for p, m in paths[caller].most_common(8):
        print(f"           {m:6d}  {p}")
