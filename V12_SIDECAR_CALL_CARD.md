# OmbreBrain V1.2 旁路调用说明卡

## 当前结论

V1.2 不直接替换主脑。当前采用旁路副脑模式。

主脑继续负责正式记忆库、pulse、breath、hold、grow、trace、post、peek、未完成事项、窗口索引卡、回响触发卡、日常跨窗交接。

V1.2 副脑负责 pinned、feel、dream、dashboard、search/bucket_id。

## 旁路副脑位置

本地测试路径：~/Desktop/Ombre-Brain1.2-clean-test

服务地址：http://localhost:8000

测试桶：buckets_test

启动命令：

cd ~/Desktop/Ombre-Brain1.2-clean-test
source .venv/bin/activate
export OMBRE_BUCKETS_DIR="/Users/yangyang/Desktop/Ombre-Brain1.2/buckets_test"
python server.py

成功标志：transport: streamable-http，并看到 Uvicorn running on http://0.0.0.0:8000

## 使用规则

普通记忆继续走主脑。

红线铁则：主脑先保留规则卡，V1.2 副脑另写 pinned。

叶辰一自己的短感受：走 V1.2 feel。只写短、准、真实的消化句，不写模板作文。

窗口收尾：主脑继续做窗口索引卡和回响触发卡，V1.2 dream 只做辅助检查。

跨窗提醒：继续走主脑 post / peek，V1.2 的 post / peek 桥只作为测试兼容。

## 已验证

V1.2 测试脑已通过 health、dashboard、pinned、feel、dream、search/bucket_id、trace、post/peek 桥、clean-test 回归。

主脑副本硬合并测试结论：post/peek 桥可补，但 pinned/feel/dream 不能直接硬塞进旧主脑，因为旧主脑缺 V1.2 的 dream 函数和新版 hold 的 pinned/feel 参数。

## 当前状态

V1.2 旁路副脑已跑通。主脑未动。

正式路线：旁路副脑，不硬合并主脑。
