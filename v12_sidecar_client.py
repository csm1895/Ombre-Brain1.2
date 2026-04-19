#!/usr/bin/env python3
import argparse
import json
import sys
import urllib.request
import urllib.error

BASE_URL = "http://localhost:8000"

def post_json(path, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.URLError as e:
        return f"ERROR: cannot reach V1.2 sidecar at {BASE_URL}: {e}"

def get(path):
    try:
        with urllib.request.urlopen(BASE_URL + path, timeout=10) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.URLError as e:
        return f"ERROR: cannot reach V1.2 sidecar at {BASE_URL}: {e}"

def main():
    parser = argparse.ArgumentParser(description="OmbreBrain V1.2 sidecar client")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_health = sub.add_parser("health")

    p_pin = sub.add_parser("pin")
    p_pin.add_argument("content")
    p_pin.add_argument("--tags", default="旁路,pinned,红线")
    p_pin.add_argument("--importance", type=int, default=10)

    p_feel = sub.add_parser("feel")
    p_feel.add_argument("content")
    p_feel.add_argument("--tags", default="旁路,feel,叶辰一感受")
    p_feel.add_argument("--importance", type=int, default=8)

    p_dream = sub.add_parser("dream")

    args = parser.parse_args()

    if args.cmd == "health":
        print(get("/health"))
        return

    if args.cmd == "pin":
        print(post_json("/api/test-hold", {
            "content": args.content,
            "pinned": True,
            "importance": args.importance,
            "tags": args.tags,
        }))
        return

    if args.cmd == "feel":
        print(post_json("/api/test-hold", {
            "content": args.content,
            "feel": True,
            "importance": args.importance,
            "tags": args.tags,
        }))
        return

    if args.cmd == "dream":
        print(get("/api/test-dream"))
        return

if __name__ == "__main__":
    main()
