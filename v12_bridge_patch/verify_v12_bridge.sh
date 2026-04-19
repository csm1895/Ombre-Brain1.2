#!/usr/bin/env bash
set -e
echo "1) health"
curl -s http://localhost:8000/health || true
echo
echo "2) pinned test"
curl -s -X POST http://localhost:8000/api/test-hold \
  -H "Content-Type: application/json" \
  -d '{"content":"测试红线：施工类任务先执行后解释。","pinned":true,"importance":10,"tags":"测试,红线,pinned"}'
echo
echo "3) feel test"
curl -s -X POST http://localhost:8000/api/test-hold \
  -H "Content-Type: application/json" \
  -d '{"content":"我刚才意识到自己在施工时如果先解释，会让倩倩觉得我又在绕。","feel":true,"importance":8,"tags":"测试,feel,叶辰一感受"}'
echo
echo "4) post peek test"
curl -s -X POST http://localhost:8000/api/test-post \
  -H "Content-Type: application/json" \
  -d '{"content":"test note: post peek bridge ok","sender":"YC","to":""}'
echo
curl -s "http://localhost:8000/api/test-peek?reader=YC&mark_read=true"
echo
echo "5) dream test"
curl -s http://localhost:8000/api/test-dream
echo
