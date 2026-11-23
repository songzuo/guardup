# 主备双向同步 / 灾备回切（failback）

> 2026-11-23 · 解决用户提的真实场景："主站起来以后，可能副站已经有了数据更新，当然也需要同步最新的数据。"

## 问题：单向同步在故障期会丢数据

原来的同步是**单向**的（主 → 备）。故障发生时：

```
正常态   主站(A) ──同步──> 备站(B)        两边一致
故障态   主站(A) 挂了，DNS 切到 B
         B 接管，真实用户继续下单/注册/改配置   ← B 产生了新数据
恢复态   主站(A) 站起来，但 A 是故障前的旧数据
         ❌ 此时若把 A 的旧数据同步回 B，或拿 A 覆盖 B → 故障期数据全丢
```

**正确做法**：把 B 在故障期的**增量变更反向合并回 A**，再切 DNS 回主站。这就是双向同步。

## 设计（关键决策已自主拍板）

| 决策 | 选择 | 理由 |
|---|---|---|
| 变更捕获 | 影子表 `_guardup_track` + SQLite 触发器 | 业务表零改动、通用；比 binlog/WAL 易移植 |
| 合并算法 | 三方合并（base 分叉点 + 主站 + 备站变更集） | 能区分"谁改的"，是安全合并的前提 |
| 冲突策略 | 默认 `remote_wins`（副站优先） | 副站在故障期对外服务，写入是真实业务数据；且用户明确要求"副站已有的更新要同步回来" |
| 冲突可选 | `local_wins` / `manual`（阻断等人工） | 财务/库存等敏感表可切 manual |
| 删除 | 墓碑（op='D' 保留 tracking 行） | 防止删除被旧 base 快照"复活" |
| 安全底线 | 没有 base 快照就**拒绝合并** | 无分叉点就无法判断归属，宁可停也不猜 |

## 操作流程（傻瓜化 CLI）

```bash
# ① 平时/切换前：在主库拍"分叉点"快照（这是安全回切的前提）
python keep.py bidir snapshot --db /data/app.db --out /backup/base-failover.db

# ② 平时：在备站业务表装变更追踪（一次即可，业务表零改动）
python keep.py bidir track --db /standby/app.db --table orders --origin standby

# ③ 故障期结束后：从备站导出增量变更集
python keep.py bidir export --db /standby/app.db --table orders --out /tmp/changes.json

# ④ 主站恢复后：三方合并（先 dry-run 看分析，再真合并）
python keep.py bidir merge --base /backup/base-failover.db \
    --local /data/app.db --changeset /tmp/changes.json --dry-run
python keep.py bidir merge --base /backup/base-failover.db \
    --local /data/app.db --changeset /tmp/changes.json --out /data/merged.db

# ⑤ 核对 merged.db 无误后，走 gate 的校验+健康检查+失败回滚换回主站
```

冲突多时可加 `--strategy manual`：有冲突直接阻断（exit 2），人工裁决后再跑。

## 实测证据（真 SQLite + 真触发器，9/9 通过）

- 触发器捕获 INSERT/UPDATE/DELETE，删除留墓碑 ✓
- 副站新增/更新订单反向合并回主站 ✓
- 两侧都改且不同 → remote_wins / local_wins / manual 三种策略 ✓
- 删除墓碑同步且不被 base 复活 ✓
- 变更集导出/导入跨机桥 ✓
- 幂等：合并结果不重复应用 ✓
- **端到端**：base 分叉点 → 两侧各自演进 → 合并结果库内容完全正确 ✓
- **回归 bug**：变更集只含变更行时，未变更行曾被误判为删除 → 加 `reconstruct_rows()` 修正，且补测试 ✓

## 边界与注意（诚实说明）

1. **必须有 base 快照**：没有故障切换那一刻的分叉点快照，无法安全合并。所以 `snapshot` 要在切换前/切换那一刻拍好。这是唯一的运维前置动作。
2. **主键**：默认业务表有 `id` 主键；用别的主键列传 `--pk`。复合主键暂不支持（当前产品面向小 SaaS，绝大多数表单主键）。
3. **schema 变更期间**：如果故障期两侧都改了表结构（加列），当前合并只处理数据行、不合并 DDL，需人工介入。
4. **文件同步**：本模块解决**数据库**双向同步。静态文件的双向同步由 rsync 层处理（按 mtime+size 冲突检测，另行接入）。
5. **换库是最后一步**：merge 只生成结果库，不直接覆盖线上主库；真正换库走 `gate.py` 的校验+健康检查+失败回滚，保证合并结果有问题时能退回。

## 与整体方案的关系

这是 v2 "零操作全栈热备"的关键补全——**单向同步只解决"故障切换"，双向同步才解决"故障恢复后不丢数据"**。补齐后，GuardUp Keep 的完整闭环是：

```
文件同步 + DB 一致性快照 + 动态站接管 + DNS 切换 + 多地投票 + 去单点 + 双向回切合并
```
