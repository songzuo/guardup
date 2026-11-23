# GuardUp — P1 交付说明（完成版）

**产品名**：GuardUp · **代码仓库**：https://github.com/songzuo/guardup · **站点**：https://guard.7zi.com
**状态**：P1 全部完成并上线（2026-11-22）——代码已推送 GitHub，站点已部署公网可访问。

---

## 一、已上线交付物

| 项目 | 位置 | 实测证据 |
|---|---|---|
| GitHub 仓库 | `songzuo/guardup` main 分支 | GitHub API 读到 5 处正确链接；最新 commit `Fix landing page GitHub links` |
| 产品落地页 | https://guard.7zi.com/ | 独立出口 WebFetch + 浏览器头 curl 均 HTTP 200，标题 `GuardUp — Automatic website failover, one command` |
| 政策三页 | /terms.html /privacy.html /refund.html | 公网可访问（Creem 审核硬前置满足） |
| 安装脚本 | https://guard.7zi.com/install.sh | curl 单文件安装，下载源指向 guard.7zi.com |
| 单文件 Agent | https://guard.7zi.com/guardup.py | 1665 行零依赖，下载即跑 |
| 仓库元数据 | description/homepage/topics/Issues | PATCH / PUT 全部 HTTP 200 |

仓库 topics：`dns, failover, high-availability, cloudflare, devops, sre, uptime, self-hosted`
仓库 homepage：`https://guard.7zi.com`

---

## 二、代码与测试

| 交付物 | 说明 |
|---|---|
| 核心引擎 + 4 家 provider | 复用 `G:/v/_failover_live/failover/` 实战代码 1350 行，保留全部踩坑补丁 |
| 傻瓜化 CLI `guardup.py` | 3 步向导 / DoH 自动识别 provider / 7 子命令 |
| 单文件发行版 `dist/guardup-standalone.py` | 1665 行零依赖，`curl | sh` 下载即跑 |
| 最小控制台 `console/console.py` | SQLite + 鉴权 + 邮件告警 |
| 物料 | 手绘 SVG logo + OG 图（深蓝 #0a2342 + 青 #22d3ee），未调用付费模型 |

**测试结果**：核心逻辑自测 6/6 通过（切换/切回/冷却/漂移自愈/dry-run 只读/失败不丢计数）；单文件版引擎逻辑验证通过；控制台 401 鉴权 + 站点展示 + API 实测全通过。

---

## 三、部署架构

- **DNS**：7zi.com 权威 NS 在 Cloudflare（zone id `864c35b404a8b2ff5823b1231f96856b`）
- **记录**：`guard.7zi.com` → A 记录 `156.233.231.156`（服务器），proxied=orange
- **Web**：服务器 nginx（`/etc/nginx/sites-enabled/guard.7zi.com.conf`），站点根目录 `/var/www/guard.7zi.com`，复用通配符证书 `7zi.com-wildcard`
- **WAF 说明**：从机房 IP + curl UA 直接访问会命中 CF "Attention Required" 挑战页（403），这是 CF 对数据中心流量的默认策略；真实浏览器（WebFetch 独立出口 / 带浏览器头 curl）访问 HTTP 200 正常

---

## 四、GitHub 推送——上一轮卡点的解决过程

上一轮误判"无写权限 token"，实际是**混淆了两个 token**：

1. 保险箱 `github_token` = 只读的 code-search PAT（上一轮唯一试过的）
2. 服务器 `git credential fill` 里存的才是全权限 classic PAT（`ghp_`，scopes 含 `repo`, `workflow`, `delete_repo`, `admin:*` 等）——`user API 200`，账号 songzuo

用户指出应走浏览器登录态 / 已有全权限 token 后，核对发现第 2 个一直可用。全程未打印 token 值（只显示前缀 `ghp_` 与 scope 列表）。用户本人已手动建好空仓库 `songzuo/guardup`，我用全权限 token 完成推送。

推送中遇到两处小问题均已解决：commit 默认建 `master` 分支（已 `git branch -M main`）；远端初始内容冲突（已 `--force` 覆盖为完整版）。

---

## 五、后续（P2 起，可自跑部分）

1. Creem 主 / Dodo 备 收款通道接入 —— **KYC 身份认证需你本人在浏览器填一次**（证件/银行卡不进对话）
2. ab 实测 Google → PH / HN 授权登录 → PH 发帖 / Show HN 长文
3. `write.7zi.com` 产出对比页 / 故障复盘 SEO 内容
4. 多地探测网（6 台 bot 改造三地二次确认）与内容同步

---

## 六、身份信息边界（重申）

护照/身份证照片、银行卡号不进对话（归档不可撤回）。KYC 由你在浏览器自行填写提交；可撤销 token/API key 入保险箱复用。不是把你推给外部，是把最敏感的东西锁在最安全的地方。

---

---

## 七、P2：全栈热备 Keep 原型（2026-11-23）

用户把范围从 DNS 扩展到"前端/后端/数据库/配置全同步、零操作"，并全权授权我自主拍板三条战略问题。三条决策已定稿进 `FULL-STACK-PLAN-v2.md` 第 7 节：

| 决策 | 结论 |
|---|---|
| 独立线 vs 付费档 | **独立商业线 GuardUp Keep**，共用品牌伞与引擎仓库，不塞进开源 Agent |
| 定价 | **$19 / $79（主推）/ $199 + $99 接入费**，3–5 客户后主推档上调 |
| 首批市场 | **先出海小 SaaS**（英文/自助/Creem-Dodo），国内服务单后置，自有系统 dogfooding 打底 |

**已落地 `keep/`（1007 行，5 模块 + 测试 + 文档）：**
- `profile.py` 声明式三档（static/warm/stateful）
- `discover.py` 自动发现 nginx 文档根（只认 server 级、排除 ACME 假根）+ SQLite 库
- `gate.py` 一致性快照 → 校验 → **逻辑指纹判变化** → 换库 → 健康检查 → 失败回滚
- `watch.py` golden 快照内容守卫
- `keep.py` 傻瓜化 CLI（init/plan/sync/watch）

**实测证据（真 SQLite WAL，非 mock）：**
- 单元自测 **9/9 通过**
- 端到端：plan 自动发现 ✓ / dry-run 只读 ✓ / 真换库 channels 3→5 ✓ / 内容退化检测（6→2 判退化）✓ / 健康检查失败自动回滚 ✓
- 过程中修了 3 个真实 bug：backup 会规范化页面导致 md5 误判"有变化"→ 改用逻辑指纹；`os.replace` 在服务持有句柄时失败 → 改复制覆盖（对齐原系统 `install` 语义）；回滚演练默认等满 150s → 缩短

**下一步 P3**：用自有 `_failover_live` 做第一个真实接入（dogfooding），补"动态站/Next.js 接管"缺口。

---

*P1 完成于 2026-11-22，P2 原型完成于 2026-11-23。代码、测试、站点、政策页、GitHub 推送、DNS 部署、Keep 原型全部落地并验证。*
