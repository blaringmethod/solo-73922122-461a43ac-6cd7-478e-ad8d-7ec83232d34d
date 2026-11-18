# 检验附件入仓桥（Lab Inbox Bridge）

共享邮箱 → 入仓作业的采集服务。通过 IMAP 读取 INBOX，把每封邮件中 1～3 个
UTF-8 CSV 检验附件（列：`报告号,样本号,项目,测值`，每件 ≤ 256 KiB，每件仅一个
报告号）持久化为不可变原件与回执，然后才把邮件标记为已读。网页与 API 可发起
采集、重试标记、查看拒收原因与“待标记”状态，并可在**来源撤销页**收回误投邮件
对报告供数的授权。

```
┌──────────┐   IMAP    ┌────────────┐   SQLite + 文件卷   ┌────────────┐
│ 共享邮箱  │◀─────────▶│ web-a      │◀──────────────────▶│ /data      │
│ INBOX    │           │ web-b      │                     │ store/     │
└──────────┘           └────────────┘◀───────────────────▶└────────────┘
                             ▲ 网页 / API（两个实例同库同卷）
```

## 快速开始

### Docker Compose（真实双实例 + 本地 IMAP 演示源）

```bash
docker compose up -d --build        # 需要本机 Docker daemon
./scripts/demo.sh                   # 逐项演示下列保证
# web-a http://localhost:8000   web-b http://localhost:8001
# 演示 IMAP localhost:1143       演示控制台 http://localhost:8081
```

Compose 启动三个容器：`demo-imap`（本地 IMAP 演示源 + 控制 API）、`web-a` 与
`web-b`（同一镜像、两个实例，共享 `shared-data` 卷中的 SQLite 与原件存储）。
两者都通过 `depends_on: service_healthy` 等到 IMAP 就绪。

### 本地（无 Docker）

```bash
pip install -r requirements.txt
python -m imap_demo.server --imap-port 1143 --control-port 8081 --data /tmp/demo-data &
INSTANCE_ID=web-a DATABASE_PATH=/tmp/bridge.sqlite3 STORAGE_DIR=/tmp/store \
IMAP_PORT=1143 IMAP_ACCOUNT=lab@demo AUTO_SCAN_INTERVAL=0 \
  python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`APP_A`/`APP_B`/`DEMO` 环境变量可让 `scripts/demo.sh` 指向任意地址。

## 交付保证与实现位置

| 需求 | 实现 |
| --- | --- |
| 只采未读会漏收 → 采集全部 UID | `Collector._list_uids` 固定 `UID SEARCH ALL`；人工已读的邮件照常入库（`tests/test_collector.py::test_pre_read_message_is_collected`） |
| 附件格式校验：UTF-8、≤256 KiB、1～3 件、每件一个报告号 | `app/mailparse.py`；表头、列数、空字段、重复报告号逐项校验 |
| 格式错误或同号异文 → 整封拒收，不留部分新报告 | 先整封校验得到 plans，再在**单个 SQLite 事务**里做冲突判定与写入；冲突时回滚并写拒收记录（`_commit_acceptance_tx` / `_Conflict`） |
| 同号同原件复用 | 以附件 SHA-256 判定；相同则 `delivery.disposition='reused'`，报告只保留一份（`report` 表唯一键 `account+mailbox+report_no`） |
| 全部附件与回执持久化后才标已读 | `Store.put_original/put_attachment/put_receipt` 均 `fsync` + 原子 rename；`_commit_acceptance` 提交后才进入 `_mark_pending` |
| 标记已生效却断连 → 不重复报告 | 标记前重新 FETCH 并比对 `raw_sha256`；IMAP 连接中断时扫描记为 `interrupted`（已提交的不回退），该行保持 `pending`，`POST /api/retry-marks` 或下次扫描补标；重新扫描按 `(account,mailbox,uidvalidity,uid)` 去重 |
| 入库后退出 / 重启 | 状态全在 SQLite + 文件卷；新进程续扫，报告幂等 |
| UIDVALIDITY 变化后重扫 | `claim_scan`/`adopt_changed_uidvalidity` 提升 `generation`、清空 `last_uid` 与租约，旧代 `pending` 标记转 `stale` |
| 复用 UID 不套旧回执 | 主键含 `uidvalidity`；标记前比对 UID 当前内容的 SHA-256，不一致则拒绝标记并转 `stale` |
| 两实例并发 / 旧扫描不推进新进度 | `mailbox_state` 中的扫描租约（token + 心跳）；`set_progress`、`finish_scan`、标记全部以 token 与 uidvalidity 为条件 |
| 标记后断连可演示 | 演示源 `POST /ctl/drop-store`：STORE 已生效但连接在回复前被关闭 |
| 网页：采集 / 重试 / 拒收原因 / 待标记 | `GET /`、`POST /actions/scan`、`POST /actions/retry-marks`、`?mark=pending` 过滤；API 见下 |
| 来源撤销：收回误投邮件的供数授权 | `GET /revocations`、`POST /actions/revoke`；`revocation` 表对 `message_id` 唯一，重复提交返回 `already` 不新增记录（`Database.revoke_message`） |
| 撤销只收授权、不删历史 | 原件、回执、`delivery` 与 `report` 行全部保留；仅插入一条 `revocation`，可用性按已提交事务实时重算，不做缓存 |
| 唯一来源被撤销 → 报告不可用、新下载拒绝 | 可用性 = 存在至少一封**未撤销的收讫邮件**交付同报告号且同字节（SHA-256）附件；`GET /api/reports/{id}/attachment` 在不可用时返回 403（`Database.report_availability`） |
| 另一封未撤销同号同字节邮件仍可供数 | 同字节交付通过 `delivery.attachment_sha256` 关联；撤销其中一封后报告保持可用，页面/API 列出实际可用来源与已撤销来源 |
| 撤销与采集交错时可用性随提交准确变化 | 撤销在单条 `BEGIN IMMEDIATE` 事务内判定并写入；读者只可能看到提交前或提交后的完整快照，不会出现混合态（`tests/test_revocation.py` 交错/压测用例） |
| 撤销失败不改变供数状态 | 目标不存在(404)/非收讫(400)/原因过短(400) 均在写入前抛 `RevocationError` 回滚；任何失败都不产生 `revocation` 行 |

### 关键不变式

1. **先落盘后标记**：文件 fsync + SQLite 提交 → 才可能发出 `STORE +FLAGS \Seen`。
2. **整封原子性**：一封邮件要么全部附件入仓，要么一条报告都不新增。
3. **消息身份 = (account, mailbox, uidvalidity, uid)**；重试与重启只更新同一条记录。
4. **旧扫描无写权限**：所有写操作都带扫描 token 与 UIDVALIDITY 条件；检测到重建即中止，且不标记、不推进 `last_uid`。
5. **标记前内容校验**：UID 当前原文 SHA-256 必须等于入库时的 SHA-256。
6. **撤销不删证、可用性实时派生**：撤销只写 `revocation`，永远不删除原件/回执/报告；报告可用性在每次读取时由 `report`/`delivery`/`revocation` 三表实时计算，因此随事务提交原子翻转，不依赖“最早来源”之类的时间假设。

## 存储布局

```
/data/bridge.sqlite3          SQLite（WAL）
/data/store/msgs/<sha256>.eml
/data/store/attachments/<报告号>/<sha256>-<文件名>.csv
/data/store/receipts/<sha256>.json
```

原件与回执均为内容寻址，重复写入天然幂等；回执包含 UID、UIDVALIDITY、原文
SHA-256、附件 SHA-256 与全部数据行，可脱离数据库核对。撤销不会移动或删除这些
文件：`revocation` 表只记录“授权收回”这一事实（原因、操作人、实例、时间），
邮件原件、回执与报告历史始终可查、可下载。

## 网页 / JSON API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/` | 仪表盘：统计、邮件记录（收讫/拒收原因/标记状态/撤销标记）、入库报告（含供数状态与实际可用来源）、扫描历史 |
| GET | `/?mark=pending` | 只看待标记 |
| GET | `/revocations` | 来源撤销页：选择收讫邮件并填写原因、撤销记录、报告供数状态与来源 |
| POST | `/actions/scan` | 页面“立即采集” |
| POST | `/actions/retry-marks` | 页面“重试待标记” |
| POST | `/actions/revoke` | 页面表单撤销（`message_id` + `reason`），303 回撤销页 |
| GET | `/api/status` | 实例、IMAP 探测、统计（含 `revocations`、`unavailable_reports`）、邮箱进度、当前租约 |
| POST | `/api/scan` | 采集；另一实例持有租约时返回 409 |
| POST | `/api/retry-marks` | 只补标记，不扫描新邮件 |
| GET | `/api/messages?mark_state=` | 邮件记录（含 `detail_list` 拒收原因、`reports`、`revocation`/`revoked`） |
| GET | `/api/messages/{id}/original` | 下载原始 RFC822 原件（撤销后仍可下载） |
| GET | `/api/messages/{id}/receipt` | 下载固定回执 JSON（撤销后仍可下载） |
| POST | `/api/messages/{id}/revoke` | 撤销收讫邮件供数授权（body：`reason`、可选 `revoked_by`）；重复提交返回 `{"status":"already"}`，不新增记录 |
| GET | `/api/revocations` | 撤销记录列表 |
| GET | `/api/reports` | 报告及实时可用性、可用/已撤销来源 |
| GET | `/api/reports/{id}` | 单报告的可用性与全部来源（含每封来源邮件的撤销状态与原因） |
| GET | `/api/reports/{id}/attachment` | 下载 CSV 原件；报告无有效来源时返回 **403** |

演示源控制 API（`demo-imap:8081`）：`POST /ctl/deliver` 投递、`POST /ctl/rebuild`
（`{"purge":false}` 重建 UIDVALIDITY 并重排 UID）、`POST /ctl/drop-store`
（下一次 STORE 生效后断连）、`GET /api/state`。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

覆盖：CSV 校验矩阵、整封拒收、同号同原件复用、同号异文冲突、预先已读、
标记后断连 + 补标记、UIDVALIDITY 重建与 UID 复用、扫描中途重建、并发租约、
重启续扫、下载接口与网页动作，以及来源撤销（`tests/test_revocation.py`）：
撤销后保留原件/回执/历史且下载 403、第二封同号同字节来源保活并显示实际来源、
重复提交幂等、失败不改变供数状态、撤销/采集事务交错的提交可见性与多轮压测。

## 配置项

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `INSTANCE_ID` | `instance` | 实例标识（租约/日志） |
| `DATABASE_PATH` | `/data/bridge.sqlite3` | 共享 SQLite 路径 |
| `STORAGE_DIR` | `/data/store` | 原件/附件/回执根目录 |
| `IMAP_HOST` / `IMAP_PORT` | `127.0.0.1` / `1143` | IMAP 地址 |
| `IMAP_ACCOUNT` | `lab@imap-demo` | 数据库中的逻辑账号键 |
| `IMAP_USER` / `IMAP_PASSWORD` | `lab` / `lab-pass` | 登录凭据 |
| `IMAP_MAILBOX` | `INBOX` | 采集文件夹 |
| `MAX_ATTACHMENT_BYTES` | `262144` | 单附件上限 |
| `MAX_ATTACHMENTS_PER_MAIL` | `3` | 单封附件数上限 |
| `AUTO_SCAN_INTERVAL` | `15`（Compose 中 `0`） | 自动扫描间隔秒；0 关闭 |
| `SCAN_LEASE_SECONDS` | `60` | 扫描租约时长 |

## 未实现 / 边界

* 生产邮箱的 TLS（`IMAPS`）与 OAuth 未接入；当前用明文 `IMAP4`，演示源亦为明文。
* 无用户认证/授权；不要把端口直接暴露到不可信网络。
* 拒收邮件按“已处理”标记 `\Seen`，不移动到隔离文件夹；重投/人工处理需在邮箱侧重发。
* 未实现附件级杀毒、压缩包展开、非 UTF-8 转码、超过 3 件邮件的拆分。
* SQLite 适合两实例共享卷；更大规模应换 PostgreSQL 与对象存储（租约与幂等键设计可直接迁移）。
* 演示 IMAP 源仅实现采集所需子集（无 TLS、无 ACL、无 IDLE）；不可用于生产。
* 撤销是单向操作（无“恢复授权”按钮）：误撤销后由对方重新投递同号同字节邮件即可恢复供数；撤销记录本身不可删除，以保留审计轨迹。
* 撤销不回写邮箱的 `\Seen` 标记、不删邮件；邮箱侧的处置仍需人工在邮件系统完成。
