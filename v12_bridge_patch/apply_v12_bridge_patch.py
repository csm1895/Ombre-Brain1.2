#!/usr/bin/env python3
from pathlib import Path

SERVER = Path("server.py")
if not SERVER.exists():
    raise SystemExit("找不到 server.py。请在 Ombre-Brain1.2 项目根目录里运行。")

s = SERVER.read_text(encoding="utf-8")

if "from starlette.responses import Response" not in s:
    s = "from starlette.responses import Response\n" + s

s = s.replace(
    'return JSONResponse({"result": result})',
    'return Response(str({"result": result}), media_type="application/json")'
)

bridge = r'''
# ============================================================
# OmbreBrain V1.2 Bridge Patch
# Adds test HTTP endpoints and post/peek compatibility layer.
# This patch is for TEST BRAIN only. Do not point it at the main bucket.
# ============================================================

def _bridge_notes_file():
    from pathlib import Path
    import os
    base = Path(os.environ.get("OMBRE_BUCKETS_DIR", "./buckets_test"))
    d = base / "_notes"
    d.mkdir(parents=True, exist_ok=True)
    return d / "notes.jsonl"

@mcp.tool()
async def post(content: str, sender: str = "YC", to: str = "") -> str:
    import json
    import uuid
    from datetime import datetime

    item = {
        "id": uuid.uuid4().hex[:12],
        "content": content,
        "sender": sender or "YC",
        "to": to or "",
        "created": datetime.now().isoformat(timespec="seconds"),
        "read_by": []
    }

    f = _bridge_notes_file()
    with f.open("a", encoding="utf-8") as out:
        out.write(json.dumps(item, ensure_ascii=False) + "\\n")

    return f"note posted: {item['id']}"

@mcp.tool()
async def peek(reader: str = "YC", mark_read: bool = True) -> str:
    import json

    f = _bridge_notes_file()
    if not f.exists():
        return "no unread notes"

    all_items = []
    unread = []

    for line in f.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        target = item.get("to", "")
        read_by = item.get("read_by", [])
        if (not target or target == reader) and reader not in read_by:
            unread.append(item)
            if mark_read:
                read_by.append(reader)
                item["read_by"] = read_by
        all_items.append(item)

    if mark_read:
        f.write_text(
            "\\n".join(json.dumps(x, ensure_ascii=False) for x in all_items) + ("\\n" if all_items else ""),
            encoding="utf-8"
        )

    if not unread:
        return "no unread notes"

    return "\\n\\n".join(
        f"NOTE {x.get('sender','')} -> {x.get('to','all') or 'all'}\\n{x.get('content','')}\\n[{x.get('created','')}]"
        for x in unread
    )

@mcp.custom_route("/api/test-hold", methods=["POST"])
async def api_test_hold(request):
    body = await request.json()
    result = await hold(
        content=body.get("content", ""),
        tags=body.get("tags", ""),
        importance=int(body.get("importance", 5)),
        pinned=bool(body.get("pinned", False)),
        feel=bool(body.get("feel", False)),
        source_bucket=body.get("source_bucket", ""),
        valence=float(body.get("valence", -1)),
        arousal=float(body.get("arousal", -1)),
    )
    return Response(str({"result": result}), media_type="application/json")

@mcp.custom_route("/api/test-trace", methods=["POST"])
async def api_test_trace(request):
    body = await request.json()
    result = await trace(
        bucket_id=body.get("bucket_id", ""),
        name=body.get("name", ""),
        domain=body.get("domain", ""),
        valence=float(body.get("valence", -1)),
        arousal=float(body.get("arousal", -1)),
        importance=int(body.get("importance", -1)),
        tags=body.get("tags", ""),
        resolved=int(body.get("resolved", -1)),
        delete=bool(body.get("delete", False)),
        pinned=int(body.get("pinned", -1)),
        digested=int(body.get("digested", -1)),
        content=body.get("content", "")
    )
    return Response(str({"result": result}), media_type="application/json")

@mcp.custom_route("/api/test-dream", methods=["POST", "GET"])
async def api_test_dream(request):
    result = await dream()
    return Response(str({"result": result}), media_type="application/json")

@mcp.custom_route("/api/test-post", methods=["POST"])
async def api_test_post(request):
    body = await request.json()
    result = await post(
        content=body.get("content", ""),
        sender=body.get("sender", "YC"),
        to=body.get("to", "")
    )
    return Response(str({"result": result}), media_type="application/json")

@mcp.custom_route("/api/test-peek", methods=["GET"])
async def api_test_peek(request):
    reader = request.query_params.get("reader", "YC")
    mark = request.query_params.get("mark_read", "true").lower() != "false"
    result = await peek(reader=reader, mark_read=mark)
    return Response(str({"result": result}), media_type="application/json")
'''

marker = '# --- Entry point / 启动入口 ---'
if "OmbreBrain V1.2 Bridge Patch" not in s:
    if marker not in s:
        raise SystemExit("找不到启动入口标记，未写入。")
    s = s.replace(marker, bridge + "\n\n" + marker)

SERVER.write_text(s, encoding="utf-8")
print("V1.2 bridge patch applied.")
