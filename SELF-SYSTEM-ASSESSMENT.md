# 自建热备系统 vs GuardUp Keep —— 同源评估与升级决策

> 评估日期 2026-11-23。证据来自实地核实：本地代码 `2026-10-24-06-41-53/deliverable7/failover/`、
> 备站 bot6（109.123.246.140）实跑状态、产品仓库 `guardup/`。不是凭印象。

## 一句话结论

**两者同源、平级互补，不是谁比谁高阶。** 自建系统是"久经实战的运维韧性专家"，
GuardUp Keep 是"架构完备但缺实战打磨的产品原型"。自建系统有 3 处真实缺口值得补，
其中 1 处（数据库同步根本没开）是致命的；产品有 4 项能力是自建系统缺的。

## 关键发现：我们其实是一套代码

逐字节 diff 的结果，不是"类似"，是**同源**：

| 文件 | 结论 |
|---|---|
| `engine.py` | 产品版 = 自建版 + 3 处增强（TCP 快路径 / TTL 地板夹住 / 每轮 TCP 预探） |
| `providers/cloudflare.py` | 完全一致 |
| `providers/aliyun.py` | 完全一致 |
| `providers/dnspod.py` | 完全一致 |
| `providers/tencentcloud.py` | 完全一致 |

产品的 DNS 引擎就是从自建系统提炼出来的，还补了自建系统踩过的坑
（TTL=60 在阿里云/DNSPod 免费套餐被拒 → 产品里强制夹到 600）。
所以"要不要把产品能力搬回自建系统"这个问题的本质是：**同源代码的正向回流**，风险极低。

## 能力对比矩阵（基于实地核实）

| 能力维度 | 自建系统（bot6 实跑） | GuardUp Keep（产品） | 谁更强 |
|---|---|---|---|
| DNS 切换引擎 | 6 目标、3 次阈值、600s 冷却、漂移自愈、幂等、dry-run；state 显示 oks=2131 轮 | 同源 + TCP 快路径 + TTL 地板 | **产品略强**（吸收了自建踩的坑） |
| 多 DNS 厂商 | CF / 阿里云 / DNSPod / 腾讯云，4 provider | 同 4 家 | 平级 |
| 站点镜像 | 6 组 rsync + nginx root 自动发现 + HTTP 兜底镜像（`mirror_http.sh` SSH 挂了用 HTTP 救站）；`/data/standby/web` **16G** 真实数据 | `discover.py` 只发现 root，无镜像编排 | **自建强很多** |
| 内容守卫 | `guard_mainlander.sh` 黄金副本 + 体积/特征/关键标记三重判定 | `watch.py` golden 快照退化检测（原型） | **自建强**（真实站点实战逻辑） |
| 恢复演练 | `restore_drill.py` 从零还原 + 起 HTTP 真比对字节 | 无 | **自建独有** |
| 备机自检 | `audit.py` 一条命令回答"备机顶不顶得住"（引擎/12 子站/API/兜底页/同步新鲜度/磁盘/离线包） | 无 | **自建独有** |
| 定时器编排 | 8+ 个 systemd timer（failover/sync/mirror/apigw/subs/cold/cert/audit/guard） | 无（CLI 手动触发） | **自建强** |
| **数据库真副本** | `SYNC_DB=none`，`/data/standby/db/` **空目录**，dump 逻辑写了但没启用 | `db.py`：一致性转储 + 校验 + PITR + lag 闸门 + 换库回滚 | **产品独有（自建致命缺口）** |
| 多地投票 / 去单点 | 单点探测（bot6 自己判自己切），无 quorum、无 fencing | `quorum.py`：6 地投票 + 跨地域法定人数 + lease 防脑裂 | **产品独有** |
| 动态应用接管 | 无（备站只反代静态站 + apigw） | `app.py`：Next.js standalone 版本化目录 + 同端口回滚，真 node 验证过 | **产品独有** |
| 双向同步 / failback | 单向 rsync（主→备），无回切合并 | `bidir.py`：三方合并 + 变更捕获 + 冲突策略 + 跨机桥 | **产品独有** |
| 证书守护 | `cert-watch.timer` + DNS-01 预签 | 无 | **自建独有** |

## 自建系统的 3 处真实缺口（按严重度）

### P0 — 数据库同步根本没开（致命）
证据：`grep SYNC_DB /etc/failover/credentials.env` 无输出；`/data/standby/db/` 空目录；
`sync.sh` 第 87 行 `SYNC_DB="${SYNC_DB:-none}"`。
也就是说：现在主站数据库一旦挂了，备站有 16G 静态文件，但**没有一个可用的数据副本**。
其它所有容灾能力都建立在"数据是活的"这个前提上，这一条塌了全塌。
> 注释里写的理由是"避免热拷贝损坏"——这个顾虑是对的，但正确解法是启用产品的
> `db.py` 一致性转储（backup API / --single-transaction），而不是干脆不开。

### P1 — 单点判定（脑裂/误判风险）
证据：`failover-check.timer` 只在 bot6 一台跑，bot6 自己探测、自己改 DNS。
如果 bot6 与目标主站之间的网络抖动（不是主站真挂），会误切；bot6 自身挂了则无人切换。
产品 `quorum.py` 的多地投票 + fencing token 正是解这个。

### P2 — 无动态站接管 + 无双向同步
证据：`standby-sites.conf` 里 Next.js 反代已下线；`sync.sh` 单向。
当前自建系统保护的多是静态站，这条优先级取决于是否有"备站也写数据"的业务。

## 升级决策（已拍板，不出选择题）

**结论：有必要升级，但方向是双向互补，不是把自建系统推倒重来换成产品。**

1. **保住自建系统的运维韧性**——`restore_drill.py`、`audit.py`、`mirror_http.sh`、
   黄金副本守卫、8 个 timer 矩阵，这些是产品在商业化时最值钱的东西（"久经实战"是护城河），
   **应该反向回流进产品**，而不是在升级里丢掉。
2. **P0 立即补**——把产品 `db.py` 的一致性转储/校验/PITR 能力移植成自建系统能用的形态。
   本轮已完成，见 `keep/db_live_sync.py`（下面详述）。
3. **P1 已备好能力层**——`quorum.py` 只写了纯逻辑，缺传输层（探测上报/租约存储）。
   本轮新增 `keep/quorum_live.py` 补齐：SQLite 共享租约（fencing CAS）+ 多地探测
   上报汇总 + 投票状态跨轮次持久化 + go/no-go 闸门。隔离验证 9/9 通过。
   **真正启用仍需跨机部署探针 + 共享库，属生产变更，未执行**（见 `QUORUM-LIVE.md`）。
4. **P2 按需**——若确认有"备站也写数据"的业务，再接 `bidir.py` 与 `app.py`。

## 本轮已落地：P0 数据库真同步补强

新增 `keep/db_live_sync.py` —— 把 `db.py` 的一致性转储理念做成自建系统可直接用的 CLI：

- 一致性快照：SQLite 走 **backup API**（不是文件拷贝，不损坏）；MySQL 走 `--single-transaction` 转储。
- 校验：magic header + integrity_check + 最小体积，坏快照绝不覆盖现库。
- PITR 目录 + manifest + 保留策略（默认 5 份）。
- 恢复演练：从任意快照还原到**临时目录**并真查询比对，不碰线上库。
- 默认 `--dry-run`，真实落盘要显式 `--apply`；全程不碰生产 `/data/standby/db/`。

测试：`keep/tests/test_db_live_sync.py`，真 SQLite 端到端，9 项全绿。

## 本轮已落地：P1 多地投票传输层补强

新增 `keep/quorum_live.py` —— 把 `quorum.py` 缺失的传输层补齐（它此前只有纯逻辑 +
测试用 `MemoryStore`，没有真实的探测上报和租约存储）：

- **SQLite 共享租约** `SQLiteLeaseStore`：实现 `get`/`set_if` 的原子 CAS
  （`BEGIN IMMEDIATE` + `UPDATE ... WHERE epoch=?`），多机争主时 fencing epoch 单调递增；
- **多地探测上报汇总** `ProbeStore`：各 bot 写 `probe_reports` 表，投票前读窗口内上报，
  喂给 `quorum.vote()`；
- **跨轮次状态持久化**：`vote_state` 表存 `bad_rounds`/`last_switch_ts` —— 这是
  "连续 N 轮确认"和"冷却"在 systemd timer 每轮独立调用时真正生效的关键
  （不持久化则每轮都从第 0 轮开始，永远切不了）；
- **fencing 闸门**：投票判死还不够，`can_operate()` 确认 actor 是当前 leader 且租约未过期
  才放行 —— 杜绝旧主脑裂双写。

安全底线：默认 dry-run，写操作显式 `--apply`；本模块只输出 go/no-go 信号，
不直接改 DNS（仍走现有 `engine.py`）；全程隔离临时目录，未碰生产与任何 bot。

测试：`keep/tests/test_quorum_live.py`，真 SQLite + 虚拟时钟，9 项全绿；
CLI 端到端演练覆盖单点误判挡住、无 leader 拒切、leader 放行、非主 fencing 拒绝、
两轮 `--apply` 完成确认累加。接线方式与边界见 `keep/QUORUM-LIVE.md`。
