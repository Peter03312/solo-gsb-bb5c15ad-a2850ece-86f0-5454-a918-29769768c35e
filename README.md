# 三角嗅辨盲评工作台（Triangle Test Bench）

基于 **FastAPI + SQLite + 原生 HTML/JS（无构建步骤）** 的 Web 应用，用于以
**三杯法（Triangle Test）** 比较两种气味样本是否可辨。

## 功能说明

**实验负责人**

1. 输入：两种样本编号（A / B）、评员名单（每行一人，自动去重）、截止时间、最少有效答卷数。
2. 系统为每位评员生成**仅本人可用、只能提交一次**的高熵盲评码与专属链接。
3. 每位评员看到三杯随机顺序：两杯同一样本、一杯另一样本；**异样杯所在位置（1/2/3）
   在全体分配中尽量均衡**，异样杯是 A 还是 B 也在全体中均衡。
4. 截止前管理台只显示「已交 / 弃权 / 有效」汇总计数，**不显示任何人的答案与杯号对应关系**。
5. 截止后一键结算，报告：
   - 有效答卷数（弃权、未交不计入）、答对数、弃权数、未交数；
   - 随机猜中概率为 1/3 时的**二项分布单侧上尾概率**
     `p = P(X ≥ 答对 | X ~ Binomial(n, 1/3))`（对数空间递推，大样本不溢出）；
   - 判定：**有效答卷数 ≥ 最少有效数 且 p ≤ 0.05 才判为“可辨”**；
     否则明确说明“样本不足（有效答卷少于最低要求）”或“证据未达阈值（p > 0.05）”。
6. 结果冻结后可回看每位评员的异样杯位置、异样样本、提交答案与对错；
   重复结算返回同一份冻结结果。
7. **截止前处理缺席（撤回 / 补位）**：凭管理凭证提交要撤回的盲评码与补位评员姓名，
   可只补位、只撤回，或在一次操作中撤回并补位：
   - 仅**未提交**者可撤回；已作答或已弃权者不可撤回；撤回后的旧码立即无法查看或提交；
   - 补位评员取得**新的专属盲评码**；同一实验中姓名（含已撤回者）不得重用；
   - 已有评员的杯序、异样样本与答卷**完全不变**；新分配在当前有效名单约束下，
     使异样杯位置与异样样本 A/B 计数尽量均衡（各档极差 ≤ 1）；
   - 调整后有效人数必须满足 **最少有效答卷数 ≤ 有效人数 ≤ 500**；
     任一校验失败则**整次操作无效**（事务回滚，不留中间态）；
   - 截止或结算后一律拒绝调整；与答卷并发时按**实际先完成**的操作判定
     （答卷先完成则撤回报「已作答」，撤回先完成则旧码立即失效、答卷按 404 拒绝）。

**评员**

- 凭专属链接或盲评码进入，只看到 1/2/3 三个杯子，嗅辨后选择异样杯，或选择**弃权**；
- **提交后不可修改、不能再次提交**；截止后服务端拒绝一切答卷；
- 结算前任何页面都看不到样本对应关系；结算后可在本人页面回看自己的分配与对错。

**并发规则**：答卷、名单调整与结算都在 SQLite `BEGIN IMMEDIATE` 写事务中完成，
截止判定一律以**服务端时钟**为准——临界时刻要么答卷落入截止前、要么按截止后拒绝；
撤回与答卷竞争同一评员时，先拿到写锁并完成的一方生效；
结算一旦写入即冻结，不会被后续操作改动。

## 一键启动（Docker Compose，推荐）

```bash
docker compose up -d --build
```

打开浏览器访问：

- 负责人首页（创建 / 进入结算台）：<http://localhost:8080/>
- 评员入口：<http://localhost:8080/eval>

停止与查看日志：

```bash
docker compose down
docker compose logs -f
```

SQLite 数据保存在 Docker 命名卷 `sniff-data`（容器内 `/data/sniff.db`），
容器重建后数据不丢失。

### 配置

| 配置项 | 方式 | 默认值 |
| --- | --- | --- |
| 宿主机访问端口 | 环境变量 `HOST_PORT`（compose 自动插值） | `8080` |
| 容器内监听端口 | Dockerfile `EXPOSE` / uvicorn 参数 | `8080` |
| SQLite 数据库路径 | 环境变量 `SNIFF_DB_PATH` | `/data/sniff.db`（卷挂载） |

改用 9090 端口示例：

```bash
HOST_PORT=9090 docker compose up -d --build
# 访问 http://localhost:9090/
```

若想把数据库放到宿主机目录而非命名卷，编辑 `docker-compose.yml` 的 volumes：

```yaml
volumes:
  - ./data:/data
```

## 本地直接运行（无 Docker）

需 Python 3.11+：

```bash
pip install -r requirements.txt
export SNIFF_DB_PATH="$(pwd)/sniff.db"
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

访问 <http://localhost:8080/>。

## 测试

测试仅依赖 Python 标准库，会启动真实 uvicorn 子进程走完整 HTTP 流程
（统计函数交叉验证、输入校验、盲态保密、一次提交、截止拒答、结算判定、冻结、回看、
撤回/补位、均衡再分配、有效人数上下限、撤回与答卷并发竞争）：

```bash
pip install -r requirements.txt
python tests/smoke_test.py
```

## HTTP API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/tests` | 创建测试，返回测试码与负责人令牌（令牌仅返回一次） |
| GET  | `/api/tests/{code}?token=` | 负责人进度（截止前仅汇总计数 + 分发链接） |
| POST | `/api/tests/{code}/adjust` | 截止前撤回盲评码 / 补位评员（可单用或合用） |
| POST | `/api/tests/{code}/settle` | 截止后结算（重复调用返回冻结结果） |
| GET  | `/api/eval/{code}` | 评员视图（结算前无任何分配/样本信息） |
| POST | `/api/eval/{code}/submit` | 提交 `{"answer":1|2|3}` 或 `{"abstain":true}` |
| GET  | `/api/time` | 服务端当前时间（毫秒） |

### 名单调整接口调用示例

`POST /api/tests/{test_code}/adjust`，请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `token` | string | 创建测试时发放的负责人令牌（必填） |
| `withdraw_codes` | string[] | 要撤回的盲评码；空/缺省表示不撤回 |
| `add_panelists` | string[] | 补位评员姓名（自动 trim、去空行）；空/缺省表示不补位 |

只补位：

```bash
curl -X POST http://localhost:8080/api/tests/AbCdEf12/adjust \
  -H 'Content-Type: application/json' \
  -d '{"token":"负责人令牌","add_panelists":["赵六","钱七"]}'
```

只撤回：

```bash
curl -X POST http://localhost:8080/api/tests/AbCdEf12/adjust \
  -H 'Content-Type: application/json' \
  -d '{"token":"负责人令牌","withdraw_codes":["kPq7mWx2Za"]}'
```

一次操作撤回并补位（整体原子，任一校验失败全部回滚）：

```bash
curl -X POST http://localhost:8080/api/tests/AbCdEf12/adjust \
  -H 'Content-Type: application/json' \
  -d '{"token":"负责人令牌",
       "withdraw_codes":["kPq7mWx2Za","nR4tYv8QbC"],
       "add_panelists":["赵六","钱七"]}'
```

成功响应（截止前仍不披露新评员的异样杯位置与异样样本，仅返回其新专属码）：

```json
{
  "ok": true,
  "test_code": "AbCdEf12",
  "withdrawn": [{"panelist": "张三", "code": "kPq7mWx2Za"}],
  "added": [
    {"panelist": "赵六", "code": "A2bcDeFGhJ", "url": "/eval/code/A2bcDeFGhJ"}
  ],
  "progress": {"total": 9, "submitted": 0, "abstained": 0, "valid": 0}
}
```

常见失败（均为整次操作无效，名单保持原状）：

| HTTP | detail 含义 |
| --- | --- |
| 403 | 管理令牌无效 |
| 404 | 测试不存在，或撤回的盲评码无效/已撤回 |
| 409 | 已作答/已弃权者不可撤回；已截止或已结算，名单不可调整 |
| 422 | 姓名重复（含与已撤回者重名）、撤回码重复、空操作、有效人数低于最少有效数或超过 500 |

管理台页面（`/admin`）在截止前也提供「名单调整」卡片，支持按行粘贴盲评码与姓名直接操作。

## 安全与使用注意

- **负责人令牌只在创建测试时返回一次**，请立即保存；它与测试码共同构成管理凭证（服务端只存 SHA-256 哈希）。
- 盲评码仅用于防混淆与一次性提交，不是身份认证：分发链接时应单独、点对点发给对应评员。
- 部署到不可信网络时建议置于 HTTPS 反向代理之后。
- 应用固定单 uvicorn worker（SQLite 单库写已串行化，满足实验规模；多 worker 也可借助 WAL 工作，但无必要）。

## 目录结构

```
app/
  main.py        # FastAPI 路由与业务逻辑
  db.py          # SQLite 连接、建表、写事务
  stats.py       # 二项分布单侧尾概率
static/
  index.html     # 负责人首页：创建 / 进入
  admin.html     # 负责人结算台：进度、分发、结算、回看
  eval.html      # 评员盲评页：三杯选择 / 弃权 / 截止后回看
  app.js, style.css
tests/smoke_test.py
Dockerfile, docker-compose.yml, requirements.txt
```
