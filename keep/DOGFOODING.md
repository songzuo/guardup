# P3 Dogfooding — Next.js 动态站接管

> 2026-11-23 · 用真 Node.js 进程验证"动态站接管"这条一直没解决的缺口。
> 隔离对照实验：临时目录 + 真 `node server.js` + 真 HTTP `/api/health`，
> 不碰生产 `_failover_live`，不误杀机器上其他 node 进程。

## 为什么这是 dogfooding 而不是 mock

现有 `_failover_live/README.md` 自述的缺口：13 个反代子域只有 503 兜底页、
`7zi.com` 无 `.next` 产物、有状态服务档为空。用户要的"啥都不做全自动"
恰恰卡在这里。本轮直接拿真实部署形态做实验：

- 实读 `7zi-frontend/next.config.ts` → `output: 'standalone'`
- 实读 `7zi-frontend/Dockerfile` → `CMD ["node", "server.js"]` + `EXPOSE 3000`
  + 健康检查 `GET /api/health` 返回 200
- Next.js 16.4.2，standalone 产物自带依赖，拷过去即可起，无需 npm install

`app.py` 把这条链自动化：产物发现 → 版本化同步 → 子进程启动 → 真 HTTP 健康
检查 → 失败切回旧版本（同端口）。

## 实测结果

### 单元层（`tests/test_app.py`，7/7 通过）
产物识别 / 指纹变化 / 版本化同步 / 健康启动 / dry-run 不起进程 /
**同端口回滚回归** / 软链切换。

### 端到端（真 node 进程，隔离端口 3212）
造 v1（`/api/health`→200）和 v2（`/api/health`→500，模拟坏发版）：

| 步骤 | 结果 |
|---|---|
| v1 启动 + 真 HTTP 探测 | `started`, healthy=True |
| 停 v1，起 v2（坏版本） | 健康检查 500 → 判定失败 |
| 自动回滚到 v1 | `rolled_back`, healthy=True, **同端口 3212** |
| 回滚后真 HTTP GET 200 | PASS |
| 进程清理 | 仅终止本次起的 2 个 node（未误杀其他） |

## 过程中修掉的 3 个真实缺陷

1. **回滚换了端口**（原 `port+1`）→ 反代仍指向坏版本，回滚无效。改成沿用同端口。
2. **`os.replace` 覆盖软链在 Windows 报 WinError 5** → 改为删旧链再建（Linux 部署无碍，兼顾本地测试）。
3. **健康检查判据**：早期版本用"进程活着"判健康，坏版本进程还在但返 500 会误判。
   固定为真实 HTTP 状态码判据 + 进程退出检测。

## 与整体方案的关系

这是 v2 方案"三个自动化缺口"的第 1 个（动态站接管）落地。剩余两个：

- 数据库真副本（PG/MySQL 逻辑复制 + PITR）—— `gate.py` 已覆盖 SQLite 档
- 引擎去单点（控制面双副本 + 多地投票）—— 待 P4

`app.py` + 已完成的 `gate.py/profile.py/discover.py/watch.py` 合起来，
已覆盖"网站级热备"的核心：文件 + SQLite 数据库 + 动态应用接管 + DNS（v1 引擎）。
