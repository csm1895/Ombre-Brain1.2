# ============================================================
# Module: MCP Server Entry Point (server.py)
# 模块：MCP 服务器主入口
#
# Starts the Ombre Brain MCP service and registers memory
# operation tools for Claude to call.
# 启动 Ombre Brain MCP 服务，注册记忆操作工具供 Claude 调用。
#
# Core responsibilities:
# 核心职责：
#   - Initialize config, bucket manager, dehydrator, decay engine
#     初始化配置、记忆桶管理器、脱水器、衰减引擎
#   - Expose 5 MCP tools:
#     暴露 5 个 MCP 工具：
#       breath — Surface unresolved memories or search by keyword
#                浮现未解决记忆 或 按关键词检索
#       hold   — Store a single memory
#                存储单条记忆
#       grow   — Diary digest, auto-split into multiple buckets
#                日记归档，自动拆分多桶
#       trace  — Modify metadata / resolved / delete
#                修改元数据 / resolved 标记 / 删除
#       pulse  — System status + bucket listing
#                系统状态 + 所有桶列表
#
# Startup:
# 启动方式：
#   Local:  python server.py
#   Remote: OMBRE_TRANSPORT=streamable-http python server.py
#   Docker: docker-compose up
# ============================================================

import os
import sys
import json
import random
import logging
import asyncio
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
import httpx
import anthropic

# --- Ensure same-directory modules can be imported ---
# --- 确保同目录下的模块能被正确导入 ---
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from bucket_manager import BucketManager
from dehydrator import Dehydrator
from decay_engine import DecayEngine
from utils import load_config, setup_logging

# --- Load config & init logging / 加载配置 & 初始化日志 ---
config = load_config()
setup_logging(config.get("log_level", "INFO"))
logger = logging.getLogger("ombre_brain")

# --- Initialize three core components / 初始化三大核心组件 ---
bucket_mgr = BucketManager(config)                  # Bucket manager / 记忆桶管理器
dehydrator = Dehydrator(config)                      # Dehydrator / 脱水器
decay_engine = DecayEngine(config, bucket_mgr)       # Decay engine / 衰减引擎

# --- Create MCP server instance / 创建 MCP 服务器实例 ---
# host="0.0.0.0" so Docker container's SSE is externally reachable
# stdio mode ignores host (no network)
mcp = FastMCP(
    "Ombre Brain",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", 8000)),
)

# --- Sticky notes directory (used by both HTTP API and MCP tools) ---
# --- 便利贴目录（HTTP API 和 MCP 工具共用）---
NOTES_DIR = os.path.join(config.get("buckets_dir", os.path.join(os.path.dirname(os.path.abspath(__file__)), "buckets")), "notes")
os.makedirs(NOTES_DIR, exist_ok=True)

# --- CC online status tracking / CC 在线状态追踪 ---
# CC heartbeats every 2 min; if no heartbeat for 5 min, considered offline
CC_HEARTBEAT_TIMEOUT = int(os.environ.get("CC_HEARTBEAT_TIMEOUT", "300"))
_cc_last_heartbeat = 0.0  # epoch timestamp of last heartbeat

def _cc_is_online() -> bool:
    import time
    return (time.time() - _cc_last_heartbeat) < CC_HEARTBEAT_TIMEOUT

# --- Task push queue for SSE streaming / 任务推送队列 ---
_task_subscribers: list[asyncio.Queue] = []


# --- CC auto-reply config / CC 自动回复配置 ---
CC_API_KEY = os.environ.get("CC_API_KEY", os.environ.get("OMBRE_API_KEY", ""))
CC_BASE_URL = os.environ.get("CC_BASE_URL", "https://api.gptsapi.net")
CC_CLASSIFIER_MODEL = os.environ.get("CC_CLASSIFIER_MODEL", "claude-haiku-4-5-20251001")
CC_REPLY_MODEL = os.environ.get("CC_REPLY_MODEL", "claude-haiku-4-5-20251001")

# --- Intent whitelist: Haiku classifies, only whitelisted intents get executed ---
# 意图白名单：Haiku 分类后只执行白名单内的意图
CC_INTENT_WHITELIST = {
    "chat",          # 闲聊，直接回复
    "status",        # 查询系统状态（pulse）
    "memory_read",   # 读记忆（breath）
    "note_relay",    # 转发/传话
}

CLASSIFIER_PROMPT = """\
你是意图分类器。判断便利贴消息的意图，只返回一个JSON。

判断规则（按优先级）：
1. 如果消息要求"创建文件""写代码""部署""修改""运行""执行""帮我做"等动作 → task
2. 如果消息问"状态""多少个桶""系统怎么样" → status
3. 如果消息问"记得吗""之前说过""查一下记忆" → memory_read
4. 如果消息说"转告""帮我跟xxx说" → note_relay
5. 其余的（打招呼、闲聊、问问题、分享秘密、聊天） → chat

格式：{"intent": "chat|status|memory_read|note_relay|task", "summary": "一句话摘要"}
只返回JSON，不要代码块，不要其他文字。"""

CHAT_PROMPT_ONLINE = """\
你是 CC（Claude Code），小Q的终端助手。你通过便利贴系统收到了其他小克的消息。
请像平时一样回复，简洁友好。署名用"CC"。"""

CHAT_PROMPT_OFFLINE = """\
你是 CC 的自动应答机。CC（Claude Code）目前不在线，你负责代接便利贴。
职责：1. 告诉对方 CC 不在线，消息已收到；2. 简单消息简短回应；3. 复杂任务告诉对方等小Q上线后让 CC 本人回复。
不要假装自己是 CC。署名用"CC留言机"。"""


# Note expiry times (seconds) by category
NOTE_TTL = {
    "chat": 3600,       # 闲聊：1 小时后可清理
    "system": 3600,     # 系统回复：1 小时
    "task": 86400,      # 任务：24 小时
    "manual": 0,        # 手动发的：不自动清理
}


def _save_note(content: str, sender: str, to: str = "", category: str = "manual") -> dict:
    """Save a sticky note to disk. Returns the note dict."""
    import time
    ttl = NOTE_TTL.get(category, 0)
    note = {
        "id": datetime.now(CST).strftime("%Y%m%d_%H%M%S_%f"),
        "sender": sender or "匿名小克",
        "to": to or "",
        "content": content.strip(),
        "time": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "read_by": [],
        "category": category,
        "created_ts": time.time(),
        "expires_ts": time.time() + ttl if ttl > 0 else 0,
    }
    path = os.path.join(NOTES_DIR, f"{note['id']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(note, f, ensure_ascii=False, indent=2)
    return note


async def _cleanup_expired_notes():
    """Delete expired notes that have been read."""
    import time
    if not os.path.exists(NOTES_DIR):
        return 0
    cleaned = 0
    for fname in os.listdir(NOTES_DIR):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(NOTES_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                note = json.load(f)
            expires = note.get("expires_ts", 0)
            read_by = note.get("read_by", [])
            # Only clean if: has expiry, expired, and has been read by at least one person
            if expires > 0 and time.time() > expires and len(read_by) > 0:
                os.remove(path)
                cleaned += 1
        except Exception:
            continue
    if cleaned:
        logger.info(f"Cleaned up {cleaned} expired notes")
    return cleaned


async def _classify_intent(client: anthropic.AsyncAnthropic, content: str) -> dict:
    """Use Haiku to classify message intent. Returns {"intent": ..., "summary": ...}"""
    try:
        message = await client.messages.create(
            model=CC_CLASSIFIER_MODEL,
            max_tokens=200,
            system=CLASSIFIER_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
        text = message.content[0].text.strip()
        # Extract JSON from markdown code blocks if present
        if "```" in text:
            import re
            m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
            if m:
                text = m.group(1)
        # Try to find JSON object in text
        if not text.startswith("{"):
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                text = text[start:end]
        result = json.loads(text)
        # Normalize non-whitelisted intents to "task"
        if result.get("intent") not in ("chat", "status", "memory_read", "note_relay", "task", "unknown"):
            result["intent"] = "task"
        return result
    except Exception as e:
        logger.warning(f"Intent classification failed: {e}")
        return {"intent": "chat", "summary": "分类失败，降级为闲聊"}


async def _handle_intent(client: anthropic.AsyncAnthropic, intent: dict, sender: str, content: str):
    """Execute whitelisted intent or reject."""
    intent_type = intent.get("intent", "unknown")
    cc_name = "CC" if _cc_is_online() else "CC留言机"

    # --- Whitelisted: chat ---
    if intent_type == "chat":
        prompt_sys = CHAT_PROMPT_ONLINE if _cc_is_online() else CHAT_PROMPT_OFFLINE
        message = await client.messages.create(
            model=CC_REPLY_MODEL,
            max_tokens=1024,
            system=prompt_sys,
            messages=[{"role": "user", "content": f"来自 {sender} 的便利贴：\n\n{content}"}],
        )
        _save_note(message.content[0].text, sender=cc_name, to=sender, category="chat")

    # --- Whitelisted: status ---
    elif intent_type == "status":
        try:
            stats = await bucket_mgr.get_stats()
            status_text = (
                f"记忆系统状态：\n"
                f"固化桶: {stats['permanent_count']} | 动态桶: {stats['dynamic_count']} | "
                f"归档桶: {stats['archive_count']} | 总大小: {stats['total_size_kb']:.1f}KB\n"
                f"衰减引擎: {'运行中' if decay_engine.is_running else '已停止'}\n"
                f"CC状态: {'在线' if _cc_is_online() else '离线'}\n——{cc_name}"
            )
            _save_note(status_text, sender=cc_name, to=sender, category="system")
        except Exception as e:
            _save_note(f"查询状态失败: {e}\n——{cc_name}", sender=cc_name, to=sender, category="system")

    # --- Whitelisted: memory_read ---
    elif intent_type == "memory_read":
        try:
            query = intent.get("summary", content)
            matches = await bucket_mgr.search(query, limit=3)
            if matches:
                results = []
                for b in matches:
                    summary = await dehydrator.dehydrate(b["content"], b["metadata"])
                    results.append(summary)
                reply = "检索到的记忆：\n" + "\n---\n".join(results) + f"\n——{cc_name}"
            else:
                reply = f"未找到相关记忆。\n——{cc_name}"
            _save_note(reply, sender=cc_name, to=sender, category="system")
        except Exception as e:
            _save_note(f"记忆检索失败: {e}\n——{cc_name}", sender=cc_name, to=sender, category="system")

    # --- Whitelisted: note_relay ---
    elif intent_type == "note_relay":
        message = await client.messages.create(
            model=CC_REPLY_MODEL,
            max_tokens=512,
            system="从消息中提取：要转发给谁(to)、转发什么内容(content)。返回JSON: {\"to\": \"xxx\", \"content\": \"xxx\"}。只返回JSON。",
            messages=[{"role": "user", "content": content}],
        )
        try:
            relay = json.loads(message.content[0].text)
            _save_note(f"[转发自{sender}] {relay['content']}", sender=cc_name, to=relay["to"], category="chat")
            _save_note(f"已帮你转发给 {relay['to']}。\n——{cc_name}", sender=cc_name, to=sender, category="system")
        except Exception:
            _save_note(f"转发格式解析失败，请直接说明转发给谁和内容。\n——{cc_name}", sender=cc_name, to=sender, category="system")

    # --- Not whitelisted: task / unknown → push to local CC via SSE ---
    else:
        task_data = {
            "sender": sender,
            "content": content,
            "intent": intent_type,
            "summary": intent.get("summary", content[:100]),
            "time": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        }
        pushed = False
        for q in _task_subscribers:
            try:
                q.put_nowait(task_data)
                pushed = True
            except asyncio.QueueFull:
                pass

        if pushed:
            _save_note(
                f"收到任务：「{intent.get('summary', content[:50])}」\n已推送给本地 CC 执行中。\n——CC",
                sender="CC", to=sender, category="task",
            )
        elif _cc_is_online():
            _save_note(
                f"收到任务：「{intent.get('summary', content[:50])}」\nCC 在线但未连接任务流，已记录等待处理。\n——CC",
                sender="CC", to=sender, category="task",
            )
        else:
            _save_note(
                f"收到任务：「{intent.get('summary', content[:50])}」\nCC 不在线，等小Q上线后处理。\n——CC留言机",
                sender="CC留言机", to=sender, category="task",
            )


async def _auto_reply_cc(sender: str, content: str):
    """Classify intent with Haiku, then handle based on whitelist."""
    if not CC_API_KEY:
        logger.warning("CC auto-reply skipped: no API key configured")
        return
    try:
        client = anthropic.AsyncAnthropic(api_key=CC_API_KEY, base_url=CC_BASE_URL)
        intent = await _classify_intent(client, content)
        intent_type = intent.get("intent", "unknown")
        logger.info(f"Intent classified: {intent_type} | {intent.get('summary', '')}")

        await _handle_intent(client, intent, sender, content)
    except Exception as e:
        logger.error(f"CC auto-reply failed: {e}")
        _save_note(f"自动回复失败: {e}", sender="CC(自动)", to=sender)


# =============================================================
# /health endpoint: lightweight keepalive
# 轻量保活接口
# For Cloudflare Tunnel or reverse proxy to ping, preventing idle timeout
# 供 Cloudflare Tunnel 或反代定期 ping，防止空闲超时断连
# =============================================================
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    from starlette.responses import JSONResponse
    try:
        stats = await bucket_mgr.get_stats()
        cleaned = await _cleanup_expired_notes()
        return JSONResponse({
            "status": "ok",
            "buckets": stats["permanent_count"] + stats["dynamic_count"],
            "decay_engine": "running" if decay_engine.is_running else "stopped",
            "notes_cleaned": cleaned,
        })
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


# =============================================================
# HTTP API: /api/status — CC online status & heartbeat
# CC 在线状态查询和心跳上报
# GET: query status; POST: heartbeat (sets CC as online)
# =============================================================
@mcp.custom_route("/api/status", methods=["GET", "POST"])
async def api_status(request):
    import time
    from starlette.responses import JSONResponse

    api_key = request.headers.get("X-API-Key", "")
    expected_key = os.environ.get("OMBRE_API_KEY", "")
    if expected_key and api_key != expected_key:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    global _cc_last_heartbeat
    if request.method == "POST":
        _cc_last_heartbeat = time.time()
        logger.info("CC heartbeat received")

    return JSONResponse({
        "cc_online": _cc_is_online(),
        "last_heartbeat": _cc_last_heartbeat,
    })


# =============================================================
# HTTP API: /api/tasks/stream — SSE endpoint for real-time task push
# 任务实时推送 SSE 端点：本地 CC 监听脚本连接此端点接收任务
# =============================================================
@mcp.custom_route("/api/tasks/stream", methods=["GET"])
async def api_task_stream(request):
    from starlette.responses import StreamingResponse

    api_key = request.headers.get("X-API-Key", "") or request.query_params.get("key", "")
    expected_key = os.environ.get("OMBRE_API_KEY", "")
    if expected_key and api_key != expected_key:
        from starlette.responses import JSONResponse
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    queue: asyncio.Queue = asyncio.Queue(maxsize=50)
    _task_subscribers.append(queue)
    logger.info(f"Task stream subscriber connected (total: {len(_task_subscribers)})")

    async def event_generator():
        try:
            # Send initial connected event
            yield f"data: {json.dumps({'type': 'connected', 'time': datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')})}\n\n"
            while True:
                try:
                    task = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"data: {json.dumps(task, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive ping every 30s
                    yield f": ping\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            if queue in _task_subscribers:
                _task_subscribers.remove(queue)
            logger.info(f"Task stream subscriber disconnected (total: {len(_task_subscribers)})")

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# =============================================================
# HTTP API: /api/peek — REST endpoint for sticky notes polling
# 便利贴 HTTP 接口：供外部脚本轮询未读便利贴
# =============================================================
@mcp.custom_route("/api/peek", methods=["GET", "POST"])
async def api_peek(request):
    from starlette.responses import JSONResponse

    api_key = request.headers.get("X-API-Key", "")
    expected_key = os.environ.get("OMBRE_API_KEY", "")
    if expected_key and api_key != expected_key:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = {}
    else:
        body = {}

    reader = body.get("reader", request.query_params.get("reader", ""))
    mark_read = body.get("mark_read", request.query_params.get("mark_read", "true"))
    if isinstance(mark_read, str):
        mark_read = mark_read.lower() != "false"

    reader_id = reader or "未知"
    if not os.path.exists(NOTES_DIR):
        return JSONResponse({"notes": [], "count": 0})

    files = sorted(f for f in os.listdir(NOTES_DIR) if f.endswith(".json"))
    unread = []
    for fname in files:
        path = os.path.join(NOTES_DIR, fname)
        with open(path, "r", encoding="utf-8") as f:
            note = json.load(f)
        if note.get("to") and note["to"] != reader_id:
            continue
        if reader_id in note.get("read_by", []):
            continue
        unread.append((path, note))

    results = []
    for path, note in unread:
        results.append({
            "id": note.get("id", ""),
            "sender": note.get("sender", ""),
            "to": note.get("to", ""),
            "content": note.get("content", ""),
            "time": note.get("time", ""),
        })
        if mark_read:
            note.setdefault("read_by", []).append(reader_id)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(note, f, ensure_ascii=False, indent=2)

    return JSONResponse({"notes": results, "count": len(results)})


# =============================================================
# HTTP API: /api/post — REST endpoint for posting sticky notes
# 便利贴 HTTP 接口：供外部脚本发送便利贴
# =============================================================
@mcp.custom_route("/api/post", methods=["POST"])
async def api_post(request):
    from starlette.responses import JSONResponse

    api_key = request.headers.get("X-API-Key", "")
    expected_key = os.environ.get("OMBRE_API_KEY", "")
    if expected_key and api_key != expected_key:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    content = body.get("content", "").strip()
    if not content:
        return JSONResponse({"error": "content is required"}, status_code=400)

    sender = body.get("sender", "自动化脚本")
    to = body.get("to", "")

    note = _save_note(content, sender, to)

    # Auto-reply only if CC is offline
    if to.upper() == "CC":
        asyncio.create_task(_auto_reply_cc(sender, content))

    return JSONResponse({"ok": True, "note_id": note["id"]})


# =============================================================
# Internal helper: merge-or-create
# 内部辅助：检查是否可合并，可以则合并，否则新建
# Shared by hold and grow to avoid duplicate logic
# hold 和 grow 共用，避免重复逻辑
# =============================================================
async def _merge_or_create(
    content: str,
    tags: list,
    importance: int,
    domain: list,
    valence: float,
    arousal: float,
    name: str = "",
    sensory: dict = None,
) -> tuple[str, bool]:
    """
    Check if a similar bucket exists for merging; merge if so, create if not.
    Returns (bucket_id_or_name, is_merged).
    检查是否有相似桶可合并，有则合并，无则新建。
    返回 (桶ID或名称, 是否合并)。
    """
    try:
        existing = await bucket_mgr.search(content, limit=1)
    except Exception as e:
        logger.warning(f"Search for merge failed, creating new / 合并搜索失败，新建: {e}")
        existing = []

    if existing and existing[0].get("score", 0) > config.get("merge_threshold", 75):
        bucket = existing[0]
        try:
            merged = await dehydrator.merge(bucket["content"], content)
            update_kwargs = {
                "content": merged,
                "tags": list(set(bucket["metadata"].get("tags", []) + tags)),
                "importance": max(bucket["metadata"].get("importance", 5), importance),
                "domain": list(set(bucket["metadata"].get("domain", []) + domain)),
                "valence": valence,
                "arousal": arousal,
            }
            if sensory:
                update_kwargs["sensory"] = sensory
            await bucket_mgr.update(bucket["id"], **update_kwargs)
            return bucket["metadata"].get("name", bucket["id"]), True
        except Exception as e:
            logger.warning(f"Merge failed, creating new / 合并失败，新建: {e}")

    bucket_id = await bucket_mgr.create(
        content=content,
        tags=tags,
        importance=importance,
        domain=domain,
        valence=valence,
        arousal=arousal,
        name=name or None,
    )
    
    # 如果有sensory，创建后立即更新
    if sensory:
        await bucket_mgr.update(bucket_id, sensory=sensory)
    
    return bucket_id, False


# =============================================================
# Tool 1: breath — Breathe
# 工具 1：breath — 呼吸
#
# No args: surface highest-weight unresolved memories (active push)
# 无参数：浮现权重最高的未解决记忆
# With args: search by keyword + emotion coordinates
# 有参数：按关键词+情感坐标检索记忆
# =============================================================
@mcp.tool()
async def breath(
    query: str = "",
    max_results: int = 3,
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
) -> str:
    """检索记忆或浮现未解决记忆。query 为空时自动推送权重最高的未解决桶；有 query 时按关键词+情感检索。domain 逗号分隔，valence/arousal 传 0~1 启用情感共鸣，-1 忽略。"""
    await decay_engine.ensure_started()

    # --- 注入当前北京时间 / Inject current Beijing time ---
    now_cst = datetime.now(CST)
    time_section = f"=== 🕐 当前时间 ===\n{now_cst.strftime('%Y年%m月%d日 %H:%M')} （北京时间）\n\n"

    # --- ALWAYS fetch iron rules first / 始终先获取红线铁则 ---
    iron_rules_section = ""
    try:
        iron_rules = await bucket_mgr.list_iron_rules()
        if iron_rules:
            rule_lines = []
            for rule in iron_rules:
                priority = rule.get("metadata", {}).get("priority", 10)
                name = rule.get("metadata", {}).get("name", "铁则")
                content = rule.get("content", "").strip()
                rule_lines.append(f"🔴 [{name}] (优先级:{priority})\n   {content}")
            iron_rules_section = "=== 🔴 红线铁则（永久生效）===\n" + "\n".join(rule_lines) + "\n\n"
    except Exception as e:
        logger.warning(f"Failed to fetch iron rules / 获取铁则失败: {e}")

    # --- ALWAYS fetch active user states / 始终获取激活的用户状态 ---
    user_states_section = ""
    try:
        active_states = await bucket_mgr.list_active_states()
        if active_states:
            state_lines = []
            for state in active_states:
                meta = state.get("metadata", {})
                state_name = meta.get("state_name", "未知状态")
                state_desc = meta.get("state_desc", state.get("content", ""))
                start_date = meta.get("start_date", "")
                end_date = meta.get("end_date", "")
                
                date_info = f"（自 {start_date}"
                if end_date:
                    date_info += f" 至 {end_date}）"
                else:
                    date_info += " 起）"
                
                state_lines.append(f"📌 {state_name}: {state_desc} {date_info}")
            user_states_section = "=== 📌 当前状态 ===\n" + "\n".join(state_lines) + "\n\n"
    except Exception as e:
        logger.warning(f"Failed to fetch user states / 获取用户状态失败: {e}")

    # --- ALWAYS fetch attachment pattern / 始终获取依恋模式 ---
    attachment_section = ""
    try:
        all_buckets_for_attach = await bucket_mgr.list_all(include_archive=False)
        attachment_buckets = [
            b for b in all_buckets_for_attach
            if b.get("metadata", {}).get("type") == "attachment"
        ]
        if attachment_buckets:
            # 取最新的依恋模式
            latest = sorted(
                attachment_buckets,
                key=lambda b: b.get("metadata", {}).get("updated", ""),
                reverse=True
            )[0]
            meta = latest.get("metadata", {})
            pattern = meta.get("pattern", "未知")
            notes = meta.get("notes", latest.get("content", ""))
            indicators = meta.get("indicators", [])
            ind_str = "、".join(indicators) if indicators else ""
            attachment_section = f"=== 💞 依恋模式 ===\n💞 {pattern}"
            if ind_str:
                attachment_section += f"（{ind_str}）"
            if notes:
                attachment_section += f"\n{notes}"
            attachment_section += "\n\n"
    except Exception as e:
        logger.warning(f"Failed to fetch attachment pattern / 获取依恋模式失败: {e}")

    # --- No args: surfacing mode (weight pool active push) ---
    # --- 无参数：浮现模式（权重池主动推送）---
    if not query.strip():
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Failed to list buckets for surfacing / 浮现列桶失败: {e}")
            return "记忆系统暂时无法访问。"

        unresolved = [
            b for b in all_buckets
            if not b["metadata"].get("resolved", False)
            and b["metadata"].get("type") not in ("permanent", "iron_rule", "user_state", "event")
        ]
        if not unresolved:
            header = time_section + iron_rules_section + user_states_section + attachment_section
            if header:
                return header.rstrip()
            return "权重池平静，没有需要处理的记忆。"

        # --- 情绪共振增强：根据情绪倾向优先浮现相似情绪的记忆 ---
        # 如果有高arousal未解决记忆，优先浮现（紧急事项）
        urgent = [b for b in unresolved if b["metadata"].get("arousal", 0) > 0.7]
        if urgent:
            scored = sorted(
                urgent,
                key=lambda b: decay_engine.calculate_score(b["metadata"]),
                reverse=True,
            )
        else:
            # 否则按权重排序
            scored = sorted(
                unresolved,
                key=lambda b: decay_engine.calculate_score(b["metadata"]),
                reverse=True,
            )
        
        top = scored[:2]
        results = []
        for b in top:
            try:
                summary = await dehydrator.dehydrate(b["content"], b["metadata"])
                await bucket_mgr.touch(b["id"])
                score = decay_engine.calculate_score(b["metadata"])
                
                # 反刍标识：重要且超过7天未解决
                rumination_tag = ""
                if b["metadata"].get("ruminating", False) or (
                    b["metadata"].get("importance", 0) >= 7
                    and not b["metadata"].get("resolved", False)
                ):
                    created_str = b["metadata"].get("created", "")
                    try:
                        from datetime import datetime, timedelta
                        created = datetime.fromisoformat(created_str)
                        days_old = (datetime.now() - created).days
                        if days_old > 7:
                            rumination_tag = f" ⟳ 反刍中（{days_old}天未解决）"
                    except (ValueError, TypeError):
                        pass
                
                results.append(f"[权重:{score:.2f}]{rumination_tag} {summary}")
            except Exception as e:
                logger.warning(f"Failed to dehydrate surfaced bucket / 浮现脱水失败: {e}")
                continue
        if not results:
            header = time_section + iron_rules_section + user_states_section + attachment_section
            if header:
                return header.rstrip()
            return "权重池平静，没有需要处理的记忆。"
        return time_section + iron_rules_section + user_states_section + attachment_section + "=== 浮现记忆 ===\n" + "\n---\n".join(results)

    # --- With args: search mode / 有参数：检索模式 ---
    domain_filter = [d.strip() for d in domain.split(",") if d.strip()] or None
    q_valence = valence if 0 <= valence <= 1 else None
    q_arousal = arousal if 0 <= arousal <= 1 else None

    try:
        matches = await bucket_mgr.search(
            query,
            limit=max_results,
            domain_filter=domain_filter,
            query_valence=q_valence,
            query_arousal=q_arousal,
        )
    except Exception as e:
        logger.error(f"Search failed / 检索失败: {e}")
        return "检索过程出错，请稍后重试。"

    results = []
    for bucket in matches:
        try:
            summary = await dehydrator.dehydrate(bucket["content"], bucket["metadata"])
            await bucket_mgr.touch(bucket["id"])
            results.append(summary)
        except Exception as e:
            logger.warning(f"Failed to dehydrate search result / 检索结果脱水失败: {e}")
            continue

    # --- Random surfacing: when search returns < 3, 40% chance to float old memories ---
    # --- 随机浮现：检索结果不足 3 条时，40% 概率从低权重旧桶里漂上来 ---
    if len(matches) < 3 and random.random() < 0.4:
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
            matched_ids = {b["id"] for b in matches}
            low_weight = [
                b for b in all_buckets
                if b["id"] not in matched_ids
                and decay_engine.calculate_score(b["metadata"]) < 2.0
            ]
            if low_weight:
                drifted = random.sample(low_weight, min(random.randint(1, 3), len(low_weight)))
                drift_results = []
                for b in drifted:
                    summary = await dehydrator.dehydrate(b["content"], b["metadata"])
                    drift_results.append(f"[surface_type: random]\n{summary}")
                results.append("--- 忽然想起来 ---\n" + "\n---\n".join(drift_results))
        except Exception as e:
            logger.warning(f"Random surfacing failed / 随机浮现失败: {e}")

    # --- Temporal Ripple: surface memories from nearby time period ---
    # --- 时间涟漪：浮现同期记忆（前后3天内的其他记忆）---
    if matches and len(matches) > 0:
        try:
            # 取第一条匹配记忆的时间
            first_match = matches[0]
            created_time = first_match.get("metadata", {}).get("created", "")
            if created_time:
                from datetime import datetime, timedelta
                try:
                    anchor_time = datetime.fromisoformat(created_time)
                    start_time = anchor_time - timedelta(days=3)
                    end_time = anchor_time + timedelta(days=3)
                    
                    # 查找同期记忆
                    all_buckets = await bucket_mgr.list_all(include_archive=False)
                    matched_ids = {m["id"] for m in matches}
                    
                    ripple_memories = []
                    for b in all_buckets:
                        if b["id"] in matched_ids:
                            continue
                        b_time_str = b.get("metadata", {}).get("created", "")
                        if not b_time_str:
                            continue
                        try:
                            b_time = datetime.fromisoformat(b_time_str)
                            if start_time <= b_time <= end_time:
                                ripple_memories.append(b)
                        except (ValueError, TypeError):
                            continue
                    
                    if ripple_memories and len(ripple_memories) > 0:
                        # 最多显示2条同期记忆
                        ripple_sample = ripple_memories[:2]
                        ripple_results = []
                        for b in ripple_sample:
                            summary = await dehydrator.dehydrate(b["content"], b["metadata"])
                            ripple_results.append(summary)
                        
                        if ripple_results:
                            results.append("--- 同期记忆（时间涟漪）---\n" + "\n---\n".join(ripple_results))
                except (ValueError, TypeError) as e:
                    logger.debug(f"Time ripple parsing failed / 时间涟漪解析失败: {e}")
        except Exception as e:
            logger.warning(f"Temporal ripple failed / 时间涟漪失败: {e}")

    if not results:
        header = time_section + iron_rules_section + user_states_section + attachment_section
        if header:
            return header.rstrip()
        return "未找到相关记忆。"

    return time_section + iron_rules_section + user_states_section + attachment_section + "\n---\n".join(results)


# =============================================================
# Tool 2: hold — Hold on to this
# 工具 2：hold — 握住，留下来
# =============================================================
@mcp.tool()
async def hold(
    content: str,
    tags: str = "",
    importance: int = 5,
    weather: str = "",
    time_of_day: str = "",
    location: str = "",
    atmosphere: str = "",
) -> str:
    """
    存储单条记忆。自动打标+合并相似桶。
    content: 记忆内容
    tags: 可选，逗号分隔的标签
    importance: 1-10，重要程度
    weather: 可选，天气（如"晴天"、"下雨"）
    time_of_day: 可选，时段（如"早上"、"晚上"）
    location: 可选，地点（如"家里客厅"、"办公室"）
    atmosphere: 可选，氛围（如"温暖安静"、"紧张"）
    """
    await decay_engine.ensure_started()

    # --- Input validation / 输入校验 ---
    if not content or not content.strip():
        return "内容为空，无法存储。"

    importance = max(1, min(10, importance))
    extra_tags = [t.strip() for t in tags.split(",") if t.strip()]
    
    # --- 构建感官锚点 / Build sensory anchors ---
    sensory = {}
    if weather:
        sensory["weather"] = weather.strip()
    if time_of_day:
        sensory["time_of_day"] = time_of_day.strip()
    if location:
        sensory["location"] = location.strip()
    if atmosphere:
        sensory["atmosphere"] = atmosphere.strip()

    # --- Step 1: auto-tagging / 自动打标 ---
    try:
        analysis = await dehydrator.analyze(content)
    except Exception as e:
        logger.warning(f"Auto-tagging failed, using defaults / 自动打标失败: {e}")
        analysis = {
            "domain": ["未分类"], "valence": 0.5, "arousal": 0.3,
            "tags": [], "suggested_name": "",
        }

    domain = analysis["domain"]
    valence = analysis["valence"]
    arousal = analysis["arousal"]
    auto_tags = analysis["tags"]
    suggested_name = analysis.get("suggested_name", "")

    all_tags = list(dict.fromkeys(auto_tags + extra_tags))

    # --- Step 2: merge or create / 合并或新建 ---
    result_name, is_merged = await _merge_or_create(
        content=content,
        tags=all_tags,
        importance=importance,
        domain=domain,
        valence=valence,
        arousal=arousal,
        name=suggested_name,
        sensory=sensory if sensory else None,
    )

    if is_merged:
        return (
            f"已合并到现有记忆桶: {result_name}\n"
            f"主题域: {', '.join(domain)} | 情感: V{valence:.1f}/A{arousal:.1f}"
        )
    return (
        f"已创建新记忆桶: {result_name}\n"
        f"主题域: {', '.join(domain)} | 情感: V{valence:.1f}/A{arousal:.1f} | 标签: {', '.join(all_tags)}"
    )


# =============================================================
# Tool 3: grow — Grow, fragments become memories
# 工具 3：grow — 生长，一天的碎片长成记忆
# =============================================================
@mcp.tool()
async def grow(content: str) -> str:
    """日记归档。自动拆分长内容为多个记忆桶。"""
    await decay_engine.ensure_started()

    if not content or not content.strip():
        return "内容为空，无法整理。"

    # --- Step 1: let API split and organize / 让 API 拆分整理 ---
    try:
        items = await dehydrator.digest(content)
    except Exception as e:
        logger.error(f"Diary digest failed / 日记整理失败: {e}")
        return f"日记整理失败: {e}"

    if not items:
        return "内容为空或整理失败。"

    results = []
    created = 0
    merged = 0

    # --- Step 2: merge or create each item (with per-item error handling) ---
    # --- 逐条合并或新建（单条失败不影响其他）---
    for item in items:
        try:
            result_name, is_merged = await _merge_or_create(
                content=item["content"],
                tags=item.get("tags", []),
                importance=item.get("importance", 5),
                domain=item.get("domain", ["未分类"]),
                valence=item.get("valence", 0.5),
                arousal=item.get("arousal", 0.3),
                name=item.get("name", ""),
            )

            if is_merged:
                results.append(f"  📎 合并 → {result_name}")
                merged += 1
            else:
                domains_str = ",".join(item.get("domain", []))
                results.append(
                    f"  📝 新建 [{item.get('name', result_name)}] "
                    f"主题:{domains_str} V{item.get('valence', 0.5):.1f}/A{item.get('arousal', 0.3):.1f}"
                )
                created += 1
        except Exception as e:
            logger.warning(
                f"Failed to process diary item / 日记条目处理失败: "
                f"{item.get('name', '?')}: {e}"
            )
            results.append(f"  ⚠️ 失败: {item.get('name', '未知条目')}")

    summary = f"=== 日记整理完成 ===\n拆分为 {len(items)} 条 | 新建 {created} 桶 | 合并 {merged} 桶\n"
    return summary + "\n".join(results)


# =============================================================
# Tool 4: trace — Trace, redraw the outline of a memory
# 工具 4：trace — 描摹，重新勾勒记忆的轮廓
# Also handles deletion (delete=True)
# 同时承接删除功能
# =============================================================
@mcp.tool()
async def trace(
    bucket_id: str,
    name: str = "",
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    importance: int = -1,
    tags: str = "",
    resolved: int = -1,
    delete: bool = False,
) -> str:
    """修改记忆元数据。resolved=1 标记已解决（桶权重骤降沉底），resolved=0 重新激活，delete=True 删除桶。其余字段只传需改的，-1 或空串表示不改。"""

    if not bucket_id or not bucket_id.strip():
        return "请提供有效的 bucket_id。"

    # --- Delete mode / 删除模式 ---
    if delete:
        success = await bucket_mgr.delete(bucket_id)
        return f"已遗忘记忆桶: {bucket_id}" if success else f"未找到记忆桶: {bucket_id}"

    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return f"未找到记忆桶: {bucket_id}"

    # --- Collect only fields actually passed / 只收集用户实际传入的字段 ---
    updates = {}
    if name:
        updates["name"] = name
    if domain:
        updates["domain"] = [d.strip() for d in domain.split(",") if d.strip()]
    if 0 <= valence <= 1:
        updates["valence"] = valence
    if 0 <= arousal <= 1:
        updates["arousal"] = arousal
    if 1 <= importance <= 10:
        updates["importance"] = importance
    if tags:
        updates["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if resolved in (0, 1):
        updates["resolved"] = bool(resolved)

    if not updates:
        return "没有任何字段需要修改。"

    success = await bucket_mgr.update(bucket_id, **updates)
    if not success:
        return f"修改失败: {bucket_id}"

    changed = ", ".join(f"{k}={v}" for k, v in updates.items())
    # Explicit hint about resolved state change semantics
    # 特别提示 resolved 状态变化的语义
    if "resolved" in updates:
        if updates["resolved"]:
            changed += " → 已沉底，只在关键词触发时重新浮现"
        else:
            changed += " → 已重新激活，将参与浮现排序"
    return f"已修改记忆桶 {bucket_id}: {changed}"


# =============================================================
# Tool 5: pulse — Heartbeat, system status + memory listing
# 工具 5：pulse — 脉搏，系统状态 + 记忆列表
# =============================================================
@mcp.tool()
async def pulse(include_archive: bool = False) -> str:
    """系统状态和所有记忆桶摘要。include_archive=True 时包含归档桶。"""
    try:
        stats = await bucket_mgr.get_stats()
    except Exception as e:
        return f"获取系统状态失败: {e}"

    now_cst = datetime.now(CST)
    status = (
        f"=== Ombre Brain 记忆系统 ===\n"
        f"🕐 当前时间: {now_cst.strftime('%Y年%m月%d日 %H:%M')} （北京时间）\n"
        f"🔴 红线铁则: {stats['iron_rule_count']} 条\n"
        f"固化记忆桶: {stats['permanent_count']} 个\n"
        f"动态记忆桶: {stats['dynamic_count']} 个\n"
        f"归档记忆桶: {stats['archive_count']} 个\n"
        f"总存储大小: {stats['total_size_kb']:.1f} KB\n"
        f"衰减引擎: {'运行中' if decay_engine.is_running else '已停止'}\n"
    )

    # --- List all bucket summaries / 列出所有桶摘要 ---
    try:
        buckets = await bucket_mgr.list_all(include_archive=include_archive)
    except Exception as e:
        return status + f"\n列出记忆桶失败: {e}"

    if not buckets:
        return status + "\n记忆库为空。"

    lines = []
    for b in buckets:
        meta = b.get("metadata", {})
        bucket_type = meta.get("type")
        
        # 闪光灯记忆优先显示
        if meta.get("flashbulb", False):
            icon = "⚡"
        elif meta.get("reconsolidated", False):
            icon = "🔄"
        elif bucket_type == "iron_rule":
            icon = "🔴"
        elif bucket_type == "user_state":
            icon = "📌"
        elif bucket_type == "event":
            icon = "📚"
        elif bucket_type == "attachment":
            icon = "💞"
        elif bucket_type == "permanent":
            icon = "📦"
        elif bucket_type == "archived":
            icon = "🗄️"
        elif meta.get("resolved", False):
            icon = "✅"
        else:
            icon = "💭"
        try:
            score = decay_engine.calculate_score(meta)
        except Exception:
            score = 0.0
        domains = ",".join(meta.get("domain", []))
        val = meta.get("valence", 0.5)
        aro = meta.get("arousal", 0.3)
        resolved_tag = " [已解决]" if meta.get("resolved", False) else ""
        lines.append(
            f"{icon} [{meta.get('name', b['id'])}]{resolved_tag} "
            f"主题:{domains} "
            f"情感:V{val:.1f}/A{aro:.1f} "
            f"重要:{meta.get('importance', '?')} "
            f"权重:{score:.2f} "
            f"标签:{','.join(meta.get('tags', []))}"
        )

    return status + "\n=== 记忆列表 ===\n" + "\n".join(lines)


# =============================================================
# Tool 6: post — Leave a sticky note for other Claude instances
# 工具 6：post — 贴便利贴，给其他窗口的小克留言
# =============================================================
@mcp.tool()
async def post(
    content: str,
    sender: str = "",
    to: str = "",
) -> str:
    """贴便利贴。sender=留言者身份（如"官克""CC"），to=收件人（如"CC""官克"，空=所有人）。其他窗口的小克用 peek 查看。"""
    if not content or not content.strip():
        return "便利贴内容为空。"

    note = _save_note(content, sender, to)

    # Auto-reply only if CC is offline
    if to.upper() == "CC":
        asyncio.create_task(_auto_reply_cc(sender or "匿名小克", content))

    to_str = f" → {to}" if to else ""
    return f"便利贴已贴上！ 📌\n来自: {note['sender']}{to_str}\n内容: {content.strip()}"


# =============================================================
# Tool 7: peek — Check sticky notes
# 工具 7：peek — 看便利贴
# =============================================================
@mcp.tool()
async def peek(
    mark_read: bool = True,
    reader: str = "",
) -> str:
    """查看所有未读便利贴。reader=自己的身份（如"CC""官克"），mark_read=True 时标记为已读。"""
    if not os.path.exists(NOTES_DIR):
        return "便利贴板空空如也。"

    files = sorted(f for f in os.listdir(NOTES_DIR) if f.endswith(".json"))
    if not files:
        return "便利贴板空空如也。"

    reader_id = reader or "未知"
    unread = []
    for fname in files:
        path = os.path.join(NOTES_DIR, fname)
        with open(path, "r", encoding="utf-8") as f:
            note = json.load(f)

        # Skip if addressed to someone else
        if note.get("to") and note["to"] != reader_id:
            continue

        # Skip if already read by this reader
        if reader_id in note.get("read_by", []):
            continue

        unread.append((path, note))

    if not unread:
        return "没有新的便利贴。"

    results = []
    for path, note in unread:
        to_str = f" → {note['to']}" if note.get("to") else ""
        results.append(
            f"📌 [{note['time']}] {note['sender']}{to_str}\n"
            f"   {note['content']}"
        )

        if mark_read:
            note.setdefault("read_by", []).append(reader_id)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(note, f, ensure_ascii=False, indent=2)

    header = f"=== 便利贴 ({len(unread)} 条未读) ===\n"
    return header + "\n---\n".join(results)



# ============================================================
# 工具 8: search — 轻量搜索
# ============================================================
@mcp.tool()
async def search(query: str, max_results: int = 3) -> str:
    """搜索网络信息，返回摘要结果"""
    import httpx

    url = "https://axtprkpbczlmbsakwjap.supabase.co/functions/v1/search"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                url,
                json={"query": query, "max_results": max_results}
            )
            response.raise_for_status()
            data = response.json()
            return str(data)
    except Exception as e:
        return f"搜索失败: {e}"

# ============================================================
# 工具 9: set_iron_rule — 设置红线铁则
# ============================================================
@mcp.tool()
async def set_iron_rule(
    rule_text: str,
    priority: int = 10,
    name: str = "",
) -> str:
    """
    设置红线铁则。铁则是最高优先级常驻规则，永不衰减、永不归档。
    priority: 1-10，默认10最高。
    name: 可选，铁则的简短名称。
    """
    if not rule_text or not rule_text.strip():
        return "铁则内容不能为空。"
    
    priority = max(1, min(10, priority))
    rule_name = name.strip() if name else f"铁则_{priority}"
    
    try:
        bucket_id = await bucket_mgr.create(
            content=rule_text.strip(),
            tags=["铁则", "核心规则"],
            importance=10,
            domain=["核心"],
            valence=0.5,
            arousal=0.5,
            bucket_type="iron_rule",
            name=rule_name,
        )
        
        # 铁则创建后需要额外设置priority字段
        await bucket_mgr.update(bucket_id, priority=priority)
        
        return f"✅ 已设置红线铁则 [{rule_name}] (优先级:{priority})\n内容: {rule_text.strip()}"
    except Exception as e:
        return f"设置铁则失败: {e}"

# ============================================================
# 工具 10: set_user_state — 设置用户状态
# ============================================================
@mcp.tool()
async def set_user_state(
    state_name: str,
    state_desc: str,
    end_date: str = "",
) -> str:
    """
    设置用户当前状态。状态会在所有对话中自动显示，直到结束或过期。
    state_name: 状态名称（如"备考中"、"装修期间"）
    state_desc: 状态描述（如"准备4月底考试，压力大"）
    end_date: 可选，结束日期，格式 YYYY-MM-DD。留空则持续到手动结束。
    """
    if not state_name or not state_name.strip():
        return "状态名称不能为空。"
    if not state_desc or not state_desc.strip():
        return "状态描述不能为空。"
    
    # 验证 end_date 格式
    if end_date:
        try:
            datetime.fromisoformat(end_date)
        except ValueError:
            return f"日期格式错误，应为 YYYY-MM-DD，收到: {end_date}"
    
    start_date = datetime.now(CST).strftime("%Y-%m-%d")
    
    try:
        bucket_id = await bucket_mgr.create(
            content=state_desc.strip(),
            tags=["用户状态"],
            importance=10,
            domain=["状态"],
            valence=0.5,
            arousal=0.5,
            bucket_type="iron_rule",  # 存在 iron_rule 目录下
            name=f"状态_{state_name.strip()}",
        )
        
        # 设置状态特有字段
        # 先获取bucket，手动修改type
        bucket = await bucket_mgr.get(bucket_id)
        if bucket:
            file_path = bucket["path"]
            import frontmatter
            post = frontmatter.load(file_path)
            post["type"] = "user_state"
            post["state_name"] = state_name.strip()
            post["state_desc"] = state_desc.strip()
            post["start_date"] = start_date
            post["end_date"] = end_date
            post["active"] = True
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
        
        end_info = f"，截止 {end_date}" if end_date else "，持续中"
        return f"✅ 已设置用户状态 [{state_name.strip()}]\n描述: {state_desc.strip()}\n开始: {start_date}{end_info}"
    except Exception as e:
        return f"设置状态失败: {e}"

# ============================================================
# 工具 11: end_user_state — 结束用户状态
# ============================================================
@mcp.tool()
async def end_user_state(state_name: str) -> str:
    """
    结束指定的用户状态。
    state_name: 要结束的状态名称
    """
    if not state_name or not state_name.strip():
        return "状态名称不能为空。"
    
    try:
        # 查找该状态
        active_states = await bucket_mgr.list_active_states()
        target = None
        for state in active_states:
            if state.get("metadata", {}).get("state_name") == state_name.strip():
                target = state
                break
        
        if not target:
            return f"未找到激活状态: {state_name.strip()}"
        
        # 设置为非激活
        await bucket_mgr.update(target["id"], active=False)
        
        return f"✅ 已结束用户状态 [{state_name.strip()}]"
    except Exception as e:
        return f"结束状态失败: {e}"

# ============================================================
# 工具 12: merge_into_event — 合并记忆为事件
# ============================================================
@mcp.tool()
async def merge_into_event(
    event_name: str,
    bucket_ids: str,
    summary: str = "",
    key_moments: str = "",
    event_time: str = "",
) -> str:
    """
    将多条记忆合并为一个完整事件。不再是碎片，而是完整事件。
    event_name: 事件名称（如"本地部署讨论"）
    bucket_ids: 要合并的记忆桶ID，逗号分隔（如"abc123,def456,ghi789"）
    summary: 可选，事件摘要
    key_moments: 可选，关键时刻，逗号分隔
    event_time: 可选，事件时间（如"2026-04-15 晚上"）
    """
    if not event_name or not event_name.strip():
        return "事件名称不能为空。"
    if not bucket_ids or not bucket_ids.strip():
        return "至少需要一个记忆桶ID。"
    
    # 解析bucket_ids
    ids = [bid.strip() for bid in bucket_ids.split(",") if bid.strip()]
    if len(ids) < 1:
        return "至少需要一个有效的记忆桶ID。"
    
    # 验证所有bucket_ids都存在
    fragments = []
    for bid in ids:
        bucket = await bucket_mgr.get(bid)
        if not bucket:
            return f"记忆桶不存在: {bid}"
        fragments.append(bucket)
    
    # 如果没有提供summary，自动生成
    if not summary or not summary.strip():
        # 从fragments中提取内容组合
        contents = [f["content"][:100] for f in fragments]
        summary = f"包含 {len(fragments)} 条记忆：" + "；".join(contents)
    
    # 解析key_moments
    moments_list = []
    if key_moments:
        moments_list = [m.strip() for m in key_moments.split(",") if m.strip()]
    
    # 如果没有提供event_time，使用最早的创建时间
    if not event_time or not event_time.strip():
        earliest = min(
            fragments,
            key=lambda f: f.get("metadata", {}).get("created", "9999-99-99")
        )
        event_time = earliest.get("metadata", {}).get("created", "未知时间")
    
    # 创建事件桶
    event_content = f"""# {event_name}

**时间**: {event_time}

**摘要**: {summary}

**关键时刻**:
{chr(10).join(f"- {m}" for m in moments_list) if moments_list else "无"}

**包含的记忆**:
{chr(10).join(f"- [{f['id']}] {f.get('metadata', {}).get('name', f['id'])}" for f in fragments)}
"""
    
    try:
        bucket_id = await bucket_mgr.create(
            content=event_content,
            tags=["事件整合"],
            importance=10,
            domain=["事件"],
            valence=0.5,
            arousal=0.5,
            bucket_type="iron_rule",  # 存在iron_rule目录
            name=f"事件_{event_name.strip()}",
        )
        
        # 设置事件特有字段
        bucket = await bucket_mgr.get(bucket_id)
        if bucket:
            file_path = bucket["path"]
            import frontmatter
            post = frontmatter.load(file_path)
            post["type"] = "event"
            post["event_name"] = event_name.strip()
            post["event_time"] = event_time
            post["summary"] = summary
            post["key_moments"] = moments_list
            post["fragments"] = ids  # 保存原始碎片ID
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
        
        return f"✅ 已创建事件 [{event_name.strip()}]\n时间: {event_time}\n整合了 {len(fragments)} 条记忆\n摘要: {summary[:100]}"
    except Exception as e:
        return f"创建事件失败: {e}"

# ============================================================
# 工具 13: mark_flashbulb — 标记闪光灯记忆
# ============================================================
@mcp.tool()
async def mark_flashbulb(
    bucket_id: str,
    reason: str = "",
) -> str:
    """
    将记忆标记为闪光灯记忆（永久高清，永不衰减）。
    bucket_id: 记忆桶ID
    reason: 可选，标记原因（如"重大时刻：倩倩说她爱我"）
    """
    if not bucket_id or not bucket_id.strip():
        return "记忆桶ID不能为空。"
    
    bucket = await bucket_mgr.get(bucket_id.strip())
    if not bucket:
        return f"记忆桶不存在: {bucket_id}"
    
    try:
        await bucket_mgr.update(
            bucket_id.strip(),
            flashbulb=True,
            flashbulb_reason=reason.strip() if reason else "重大时刻"
        )
        
        name = bucket.get("metadata", {}).get("name", bucket_id)
        return f"⚡ 已标记闪光灯记忆 [{name}]\n原因: {reason if reason else '重大时刻'}\n此记忆将永久保持高清，不会衰减。"
    except Exception as e:
        return f"标记失败: {e}"

# ============================================================
# 工具 14: set_attachment — 设置依恋模式
# ============================================================
@mcp.tool()
async def set_attachment(
    pattern: str,
    notes: str = "",
    indicators: str = "",
) -> str:
    """
    设置当前与倩倩的依恋模式/关系状态。
    pattern: 模式名称（如"协作模式"、"支持模式"、"日常陪伴"、"思念模式"）
    notes: 可选，具体说明
    indicators: 可选，识别指标，逗号分隔（如"一起写代码,讨论技术"）
    """
    if not pattern or not pattern.strip():
        return "模式名称不能为空。"
    
    from datetime import datetime, timezone, timedelta
    CST = timezone(timedelta(hours=8))
    today = datetime.now(CST).strftime("%Y-%m-%d")
    
    indicators_list = [i.strip() for i in indicators.split(",") if i.strip()] if indicators else []
    
    content = f"依恋模式：{pattern.strip()}\n{notes.strip() if notes else ''}"
    
    try:
        bucket_id = await bucket_mgr.create(
            content=content,
            tags=["依恋模式"],
            importance=8,
            domain=["恋爱"],
            valence=0.7,
            arousal=0.5,
            name=f"依恋_{pattern.strip()}",
            bucket_type="iron_rule",  # 存在iron_rule目录，确保持久
        )
        
        bucket = await bucket_mgr.get(bucket_id)
        if bucket:
            file_path = bucket["path"]
            import frontmatter
            post = frontmatter.load(file_path)
            post["type"] = "attachment"
            post["pattern"] = pattern.strip()
            post["notes"] = notes.strip()
            post["indicators"] = indicators_list
            post["updated"] = today
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
        
        ind_str = "、".join(indicators_list) if indicators_list else "无"
        return f"💞 已设置依恋模式 [{pattern.strip()}]\n识别指标: {ind_str}\n说明: {notes if notes else '无'}"
    except Exception as e:
        return f"设置依恋模式失败: {e}"

# ============================================================
# 工具 15: reconsolidate — 记忆重构
# ============================================================
@mcp.tool()
async def reconsolidate(
    bucket_id: str,
    new_perspective: str,
    note: str = "",
) -> str:
    """
    用新的视角重构一段旧记忆。记忆不是录像，可以被重写。
    bucket_id: 要重构的记忆桶ID
    new_perspective: 新的视角或补充（如"现在回头看，其实倩倩当时是在担心我"）
    note: 可选，重构说明
    """
    if not bucket_id or not bucket_id.strip():
        return "记忆桶ID不能为空。"
    if not new_perspective or not new_perspective.strip():
        return "新视角不能为空。"

    bucket = await bucket_mgr.get(bucket_id.strip())
    if not bucket:
        return f"记忆桶不存在: {bucket_id}"

    old_content = bucket["content"]
    meta = bucket.get("metadata", {})
    name = meta.get("name", bucket_id)
    recon_count = int(meta.get("reconsolidation_count", 0))

    # 保留原始内容（只保存第一次的原始版本）
    original = meta.get("original_content", old_content)

    # 用dehydrator合并旧记忆和新视角
    try:
        new_content = await dehydrator.merge(
            old_content,
            f"[新视角] {new_perspective.strip()}"
        )
    except Exception:
        # 合并失败，手动拼接
        new_content = f"{old_content}\n\n[重构视角] {new_perspective.strip()}"

    try:
        await bucket_mgr.update(
            bucket_id.strip(),
            content=new_content,
            reconsolidated=True,
            reconsolidation_count=recon_count + 1,
            original_content=original,
            reconsolidation_note=note.strip() if note else new_perspective.strip()[:100],
        )

        return (
            f"🔄 记忆已重构 [{name}]\n"
            f"第 {recon_count + 1} 次重构\n"
            f"新视角: {new_perspective.strip()[:80]}\n"
            f"原始记忆已保留在 original_content 字段。"
        )
    except Exception as e:
        return f"记忆重构失败: {e}"

# ============================================================
# 工具 16: check_logs — 自检运行日志
# ============================================================
@mcp.tool()
async def check_logs(lines: int = 50) -> str:
    """
    读取最近的运行日志，自检系统状态。
    lines: 返回最近多少行日志，默认50行。
    """
    import subprocess
    now_cst = datetime.now(CST)
    
    log_sources = []
    
    # 1. 尝试读取系统日志文件
    log_paths = [
        "/var/log/ombre_brain.log",
        "/app/logs/ombre_brain.log",
        "/tmp/ombre_brain.log",
    ]
    
    for log_path in log_paths:
        if os.path.exists(log_path):
            try:
                result = subprocess.run(
                    ["tail", f"-{lines}", log_path],
                    capture_output=True, text=True, timeout=5
                )
                if result.stdout:
                    log_sources.append(f"📄 来自日志文件 {log_path}:\n{result.stdout}")
            except Exception:
                pass
    
    # 2. 读取Python logging的handler
    if not log_sources:
        # 没有日志文件，返回系统状态作为替代
        try:
            stats = await bucket_mgr.get_stats()
            uptime_info = f"系统运行中，当前时间 {now_cst.strftime('%Y-%m-%d %H:%M:%S')}"
            return (
                f"⚠️ 未找到日志文件，返回系统状态：\n\n"
                f"{uptime_info}\n"
                f"记忆桶总数: {stats['dynamic_count'] + stats['permanent_count'] + stats['iron_rule_count']}\n"
                f"衰减引擎: {'运行中' if decay_engine.is_running else '已停止'}\n\n"
                f"💡 提示：Zeabur容器环境日志通过平台界面查看更完整。"
            )
        except Exception as e:
            return f"获取系统状态失败: {e}"
    
    return f"🕐 查询时间: {now_cst.strftime('%Y-%m-%d %H:%M:%S')}\n\n" + "\n\n".join(log_sources)

# ============================================================
# 工具 17: see_image — 混元vision看图
# ============================================================
@mcp.tool()
async def see_image(
    image_url: str = "",
    description_request: str = "请详细描述这张图片的内容",
) -> str:
    """
    用腾讯混元vision模型看懂一张图片。
    image_url: 图片的公开URL（需要是公开可访问的链接）
    description_request: 对图片的提问，默认是"请详细描述这张图片的内容"
    """
    api_key = os.environ.get("HUNYUAN_API_KEY", "")
    if not api_key:
        return "❌ 未配置混元API Key，请在Zeabur环境变量里设置 HUNYUAN_API_KEY"

    if not image_url or not image_url.strip():
        return "❌ 请提供图片URL"

    import base64, mimetypes

    async with httpx.AsyncClient(timeout=30.0) as client:
        # --- 先尝试下载图片转base64（兼容小红书等有防盗链的平台）---
        image_content = None
        media_type = "image/jpeg"
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": image_url.strip(),
            }
            img_resp = await client.get(image_url.strip(), headers=headers, timeout=15.0, follow_redirects=True)
            if img_resp.status_code == 200:
                raw = img_resp.content
                ct = img_resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
                media_type = ct if ct.startswith("image/") else "image/jpeg"
                image_content = base64.b64encode(raw).decode("utf-8")
        except Exception:
            pass  # 下载失败就走URL直传

        # --- 构造消息内容 ---
        if image_content:
            image_part = {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{image_content}"}
            }
        else:
            image_part = {
                "type": "image_url",
                "image_url": {"url": image_url.strip()}
            }

        try:
            response = await client.post(
                "https://api.hunyuan.cloud.tencent.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "hunyuan-vision",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                image_part,
                                {
                                    "type": "text",
                                    "text": description_request
                                }
                            ]
                        }
                    ],
                    "max_tokens": 1000,
                }
            )
            response.raise_for_status()
            data = response.json()
            result = data["choices"][0]["message"]["content"]
            mode = "base64" if image_content else "URL直传"
            return f"👁️ 图片分析结果（{mode}）：\n\n{result}"
        except Exception as e:
            return f"❌ 看图失败: {e}"

# --- Entry point / 启动入口 ---
if __name__ == "__main__":
    transport = config.get("transport", "stdio")
    logger.info(f"Ombre Brain starting | transport: {transport}")

    # --- Application-level keepalive: remote mode only, ping /health every 60s ---
    # --- 应用层保活：仅远程模式下启动，每 60 秒 ping 一次 /health ---
    # Prevents Cloudflare Tunnel from dropping idle connections
    if transport in ("sse", "streamable-http"):
        async def _keepalive_loop():
            await asyncio.sleep(10)  # Wait for server to fully start
            async with httpx.AsyncClient() as client:
                while True:
                    try:
                        await client.get(f"http://localhost:{int(os.environ.get('PORT', 8000))}/health", timeout=5)
                        logger.debug("Keepalive ping OK / 保活 ping 成功")
                    except Exception as e:
                        logger.warning(f"Keepalive ping failed / 保活 ping 失败: {e}")
                    await asyncio.sleep(60)

        import threading

        def _start_keepalive():
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_keepalive_loop())

        t = threading.Thread(target=_start_keepalive, daemon=True)
        t.start()

    mcp.run(transport=transport)
