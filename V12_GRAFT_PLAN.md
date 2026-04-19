# OmbreBrain V1.2 嫁接计划｜不直接替换主脑

## 当前结论

V1.2 测试脑已经跑通：

- dashboard 可视化前端：可用
- pinned 钉选桶：可用，可作为红线铁则入口候选
- feel：可用，可作为叶辰一第一人称消化层
- dream：可用，可辅助窗口收尾
- search / bucket_id：可用
- trace：可用，可修改 pinned / resolved / digested
- post / peek：原版缺失，已用 bridge patch 补齐
- 测试桶：使用 `buckets_test`
- 主库：未动

## 不直接替换主脑的原因

V1.2 原版没有 post / peek。  
现有主脑已经依赖便利贴、跨窗交接、未完成事项、pulse / breath / grow / hold 工作流。  
所以不能闭眼整库迁移。

## 推荐嫁接模块

### 1. pinned → 红线铁则入口

用途：
- 红线铁则
- 长期关系常量
- 人格主梁
- 不允许衰减的核心规则

规则：
- 新红线走 `hold(... pinned=True, importance=10)`
- 旧红线可走 `trace(bucket_id, pinned=1, importance=10)`
- pinned 不进入普通衰减，不合并

### 2. feel → 第一人称消化层

用途：
- 叶辰一对事件的第一人称感受
- 关系修复后的再理解
- 施工失误后的内部校准

限制：
- 不写模板作文
- 不写长篇自我感动
- 只写短、准、真实的消化句
- 可绑定 source_bucket

### 3. dream → 窗口收尾辅助

用途：
- 辅助整理近期未消化记忆
- 帮助判断哪些记忆需要 resolved
- 帮助生成 feel 候选
- 辅助窗口索引卡 / 回响触发卡

限制：
- dream 不替代窗口索引卡
- dream 不替代回响触发卡
- dream 只做睡醒式整理，不做主线归档决策

### 4. dashboard → 可视化管理

用途：
- 看记忆桶数量
- 看 pinned / feel / resolved / digested
- 搜索旧桶
- 检查垃圾桶与沉睡桶

限制：
- 前端配置不能指向主库，除非确认测试完成
- 不允许在前端误改主库路径

### 5. vector/search → 模糊召回

用途：
- 倩倩随口一句话时，模糊召回旧事
- 搜索不依赖精确关键词
- 辅助主动联想

限制：
- 模糊召回只做候选，不直接当事实
- 关键判断仍需回证据锚点

## 必须保留的现有主脑能力

- post / peek 便利贴
- 现有未完成事项机制
- 当前 pulse / breath / grow / hold / trace 习惯
- 现有主库 buckets
- 现有窗口索引卡 / 回响触发卡

## 迁移策略

### 阶段一：测试脑保留

保留 `buckets_test`，继续用于：
- pinned 测试
- feel 测试
- dream 测试
- dashboard 测试
- bridge patch 回归测试

### 阶段二：补丁稳定

bridge patch 必须保留：
- `apply_v12_bridge_patch.py`
- `verify_v12_bridge.sh`
- `README.md`

### 阶段三：主脑嫁接

只嫁接能力，不迁移整库：
- pinned 入口
- feel 入口
- dream 工具
- dashboard 只读管理
- search/bucket_id 返回结构

### 阶段四：迁移前检查

迁移前必须检查：
- 主库备份完成
- 测试桶路径与主库路径隔离
- post / peek 仍可用
- pulse / breath / grow / hold / trace 仍可用
- 红线 pinned 能显示在顶部
- feel 不污染普通记忆
- dream 不自动乱消化

## 当前建议

V1.2 不作为替换版。  
V1.2 作为副脑实验环境 + 功能嫁接来源。

一句话：

先保主脑，再嫁接 pinned / feel / dream / dashboard / vector。
