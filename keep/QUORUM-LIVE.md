# P1 多地投票补强 —— 自建系统去单点

> 落地日期 2026-11-23。对应 `SELF-SYSTEM-ASSESSMENT.md` 的 P1 缺口。
> 代码 `quorum_live.py`，测试 `tests/test_quorum_live.py`（9/9）。

## 解决什么问题

自建系统现在的切换决策是**单点**：`failover-check.timer` 只在 bot6 一台跑，
bot6 自己探测目标、自己改 DNS。两个真实风险：

1. bot6 到主站之间的网络抖动（主站没真挂）→ bot6 误判 → 错误切换。
2. bot6 自身挂了 → 没人切换，容灾失效。

产品 `quorum.py` 早就把解法写好了（多地投票 + fencing 租约），但它的注释里
明说"真实部署：探测上报来自 6 台 bot 的 HTTP 心跳，租约存 SQLite/etcd" ——
**这层传输实现一直空缺**，只有测试用的 `MemoryStore`。本模块补齐这层。

## 三个组件

| 组件 | 作用 | 关键实现 |
|---|---|---|
| `SQLiteLeaseStore` | 满足 `quorum.Lease` 的 store 接口（get / set_if） | `set_if` 用 `BEGIN IMMEDIATE` + `UPDATE ... WHERE epoch=?` 的 rowcount 做原子 CAS，多机共享一个 SQLite 文件时 fencing epoch 单调递增 |
| `ProbeStore` | 各 bot 探测上报汇总 + 投票状态持久化 | `probe_reports` 表存上报；`vote_state` 表存 bad_rounds/last_switch_ts —— **这是连续确认和冷却在 timer 每轮独立调用时能生效的关键** |
| `decide()` / `can_operate()` | 读上报→投票→fencing 闸门 | 投票判死还不够，必须确认 actor 是当前 leader 且租约未过期，`can_operate=True` 才允许改 DNS |

**逻辑算法完全复用 `quorum.py`，这里只做传输层和持久化，没有重写投票/fencing。**

## 与自建系统接线（部署时用，本轮未动生产）

设计的接入点，真正启用时按此接线（需要跨机部署探针，属业务动作，未擅自执行）：

```
每台 bot（含 bot6）加一个 timer，每 60s：
  python quorum_live.py report --db <共享库> --node bot-XX --region <地域> \
      --healthy <探测结果 0/1> --apply

控制面（争主的那台）每轮：
  python quorum_live.py lease  --db <共享库> --holder bot-XX --action renew --epoch N --apply
  python quorum_live.py vote   --db <共享库> --confirm-rounds 2 --actor bot-XX --apply
  → 解析 verdict==switch 且 can_operate==true 时，才调用现有 engine.py 改 DNS
```

- 共享库路径：放在所有 bot 都能访问的位置（NFS 挂载 / 专用小卷）。SQLite 的
  `BEGIN IMMEDIATE` 在同机多进程下可靠；跨机共享需文件系统支持锁（NFSv4 可用，
  不支持则用 etcd 替换 store —— 接口已抽象，换实现即可）。
- `verdict` 取值：`hold` / `no_quorum` / `recovering` / `no_leader` / `switch`。
  只有 `switch` 且 `can_operate=true` 是两个条件同时满足的放行信号。

## 与传统单点引擎的行为差异（都是刻意的保护）

| 场景 | 旧单点引擎 | 新投票判定 |
|---|---|---|
| 单点报故障 | 立即切 | `hold`（多数健康，漂移自愈），不切 |
| 同机房 2 点报故障 | 可能切 | `no_quorum`（地域不足），不切 |
| 判死但自己不是 leader | 自行切 | `no_leader`，拒绝 —— 防脑裂双写 |
| 刚切过又抖 | 可能来回切 | `recovering`（冷却期抑制） |
| leader 租约过期 | 无此概念 | `can_operate=false`，旧主自动失效 |

## 验证证据

- `tests/test_quorum_live.py` **9/9**：真 SQLite 租约 CAS、过期接管、续租 CAS、
  上报窗口/最新、跨轮次状态持久化（★）、单点误判挡住、漂移自愈、fencing 双节点、
  过期闸门。
- CLI 端到端演练：单点误判→hold；无 leader 判死→no_leader；leader 判死→switch+
  放行；非主→拒绝(exit=3)；dry-run 不写库；两轮 `--apply` 完成确认累加。
- 全程隔离临时目录，**未碰生产** `/etc/failover`、任何 bot、DNS 解析。

## 边界（诚实说明）

1. 本模块只输出 go/no-go 信号，**不直接调 DNS provider**；真正切换仍由现有
   `engine.py` 执行，接线在上面。这样保留了自建系统成熟的切换逻辑。
2. 跨机共享 SQLite 依赖文件锁；纯 NFSv3/对象存储锁语义弱，那种环境应把
   `SQLiteLeaseStore` 换成 etcd 实现（接口一致，改动局限在 store 层）。
3. 探测本身（HTTP/TCP 探活、阈值）沿用自建系统现有的探测脚本，本模块只负责
   汇总与决策；`report --healthy` 的值由探测方给出。
4. 未做跨机部署、未启用 timer —— 那是需要动多台 bot 的生产变更，等 P0/P1
   在隔离环境跑稳、你确认拓扑后再做。
