# OmbreBrain V1.2 Bridge Patch｜测试脑桥接补丁

用途：
- 给 V1.2 测试脑补 `post / peek` 便利贴桥。
- 给本地测试补 HTTP 入口：`/api/test-hold`、`/api/test-trace`、`/api/test-dream`、`/api/test-post`、`/api/test-peek`。
- 只用于测试脑，不要直接指向主库。

运行位置：
在 `OmbreBrain_V12_test_ready` 项目根目录运行。

命令：

```bash
python3 apply_v12_bridge_patch.py
export OMBRE_BUCKETS_DIR="$(pwd)/buckets_test"
python server.py
```

测试 post/peek：

```bash
curl -X POST http://localhost:8000/api/test-post \
  -H "Content-Type: application/json" \
  -d '{"content":"test note: post peek bridge ok","sender":"YC","to":""}'

curl "http://localhost:8000/api/test-peek?reader=YC&mark_read=true"
```

测试 pinned：

```bash
curl -X POST http://localhost:8000/api/test-hold \
  -H "Content-Type: application/json" \
  -d '{"content":"测试红线：施工类任务先执行后解释。","pinned":true,"importance":10,"tags":"测试,红线,pinned"}'
```

测试 feel：

```bash
curl -X POST http://localhost:8000/api/test-hold \
  -H "Content-Type: application/json" \
  -d '{"content":"我刚才意识到自己在施工时如果先解释，会让倩倩觉得我又在绕。","feel":true,"importance":8,"tags":"测试,feel,叶辰一感受"}'
```

测试 dream：

```bash
curl http://localhost:8000/api/test-dream
```

测试 trace：

```bash
curl -X POST http://localhost:8000/api/test-trace \
  -H "Content-Type: application/json" \
  -d '{"bucket_id":"替换成真实id","name":"测试红线-pinned确认","pinned":1,"importance":10,"tags":"测试,红线,pinned,trace确认"}'
```

已知结论：
- V1.2 的 pinned 可以作为红线入口候选。
- feel 可以作为叶辰一第一人称消化层。
- dream 可辅助窗口收尾，但不替代窗口索引卡 / 回响触发卡。
- V1.2 原版没有 post / peek，所以需要这个桥。
