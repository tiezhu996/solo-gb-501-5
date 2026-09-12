# 无菌包装环境监测与批次放行联动子系统

面向无菌包装车间的环境监测（EMS）与批次放行联动系统：监测点关联产线与限值，登记温湿度、压差、悬浮粒子读数；读数超限自动生成事件并拦截关联批次提交放行；质量人员记录原因与措施、复测合格后方可关闭事件。

零依赖（Python 3.8+ 标准库 + SQLite + 原生前端），无需安装任何第三方包。

## 快速启动

```bash
cd sterile-env-monitoring
python3 server.py            # 默认端口 8000，PORT 环境变量可覆盖
# 浏览器访问 http://localhost:8000
```

首次启动自动建库并写入演示数据（幂等，已有数据不覆盖）。重置演示数据：

```bash
python3 server.py --reset
```

端到端业务规则校验（独立临时实例，不影响正式数据）：

```bash
python3 verify.py            # 34 项断言全部通过则退出码为 0
```

校验覆盖三部分：API 业务规则（拦截/复测/关闭/放行/输入校验）、页面真实渲染
（`verify_pages.js` 以 DOM 桩加载真实 `static/app.js`，对运行中的服务逐页断言
接口数据出现在页面上）、并发安全（8 线程并发放行同一批次，断言仅 1 次成功、
其余收到已放行提示）。页面校验也可单独执行：

```bash
EMR_BASE=http://127.0.0.1:8000 node verify_pages.js
```

## 业务规则

1. **监测点**：关联产线，参数为温度 / 湿度 / 压差 / 悬浮粒子，限值支持上下限或单边（压差 ≥、粒子 ≤）。
2. **读数登记**：常规读数超限时**自动生成事件**，事件关联该产线**当前在产批次**；产线无在产批次时事件不关联批次。
3. **放行拦截**：批次存在未关闭事件（含复测仍超限）时，`提交放行` 返回 `409 BATCH_BLOCKED` 并列出拦截事件。
4. **事件处置**：质量人员记录原因与纠正措施（未记录不能关闭）。
5. **复测与关闭**：只有在事件下登记的**复测读数合格**后才能关闭事件；复测仍超限则事件保持未关闭并继续拦截。常规读数不改变事件状态。
6. **已放行批次保护**：事件只关联在产批次，已放行批次永远不能被补挂事件；重复放行返回 `409 ALREADY_RELEASED`。
7. **并发安全**：放行与事件关闭采用原子条件更新（`UPDATE ... WHERE status=...`）+ `busy_timeout`，并发提交同一批次仅一个请求成功，其余收到 `409 ALREADY_RELEASED`（事件关闭同理返回 `409 EVENT_CLOSED`）。

## 演示数据（覆盖四类场景）

| 场景 | 数据 |
| --- | --- |
| 正常 | 全部监测点 09-11/09-12 早班合格读数；批次 B2026-0903 无任何事件 |
| 超限（拦截中） | 温度 28.4℃（限 26）→ **EV-0002 未关闭**，已记录原因措施，复测 27.1℃ 仍超限 → **B2026-0901 被拦截** |
| 关闭 | 粒子 3860（限 3520）→ **EV-0001**，记录原因措施、复测 2900 合格后**已关闭** → B2026-0902 可放行 |
| 已放行 | B2026-0831 已放行，不能被补挂事件、不能重复放行 |

推荐演示路径：

1. 「总览」查看未关闭事件与批次放行状态（B2026-0901 被事件拦截）。
2. 「批次放行」点击 B2026-0901 的 **被拦截（1）** → 红色拦截面板列出 EV-0002，可跳转事件详情。
3. 「超限事件」打开 EV-0002 → 登记复测 `23.0` → 提示满足关闭条件 → **关闭事件**。
4. 回到「批次放行」提交 B2026-0901 → 放行成功。
5. 「读数登记」选 `P-TEMP-02` 填 `29` → 自动生成事件并关联 B2026-0903（已放行的 B2026-0831 不受影响）→ B2026-0903 随即被拦截。

## 页面

| 页面 | 功能 |
| --- | --- |
| `#overview` 总览 | 统计卡片、未关闭事件、各批次放行判定、最近事件 |
| `#points` 监测点 | 监测点列表（产线/参数/限值）与新增 |
| `#readings` 读数登记 | 登记读数（实时限值提示），超限即时反馈事件号与关联批次；最近读数表 |
| `#events` 超限事件 | 状态筛选、事件详情、原因措施记录、复测登记、关闭（不满足条件时展示阻塞原因） |
| `#batches` 批次放行 | 批次列表与未关闭事件数、提交放行（被拦截时列出事件）、新建批次 |

## API 一览

统一响应：成功 `{"ok": true, "data": ...}`；失败 `{"ok": false, "error": {code, message, details}}`。

| 方法与路径 | 说明 |
| --- | --- |
| `GET /api/health` | 健康检查 |
| `GET /api/overview` | 总览聚合（计数、批次放行判定、未关闭/最近事件） |
| `GET /api/lines` | 产线列表（含监测点数、在产批次数） |
| `GET /api/points` · `POST /api/points` | 监测点查询 / 创建（`line_id, code, name, parameter, unit, limit_min?, limit_max?`） |
| `GET /api/readings` · `POST /api/readings` | 读数查询 / 登记（`point_id, value, recorded_by`；带 `event_id` 时为该事件的复测） |
| `GET /api/events` · `GET /api/events/:id` | 事件列表（`status`/`batch_id` 过滤）/ 详情（含复测记录） |
| `POST /api/events/:id/disposition` | 记录原因与措施（`cause, measures, operator`） |
| `POST /api/events/:id/close` | 关闭事件（校验原因措施 + 最近复测合格） |
| `GET /api/batches` · `POST /api/batches` | 批次查询（含未关闭事件数、可否放行）/ 创建 |
| `POST /api/batches/:id/submit-release` | 提交放行；被拦截时 `409` 且 `details.blocking_events` 列出事件 |

示例：

```bash
# 登记超限读数（自动生成事件并关联在产批次）
curl -s http://localhost:8000/api/readings -H 'Content-Type: application/json' \
  -d '{"point_id": 5, "value": 29.0, "recorded_by": "李监测"}'

# 被拦截的批次提交放行
curl -s -X POST http://localhost:8000/api/batches/1/submit-release \
  -H 'Content-Type: application/json' -d '{"operator":"王质量"}'
```

## 结构

```text
sterile-env-monitoring/
├── server.py        # HTTP 服务、路由、全部业务规则（含原子状态迁移）
├── db.py            # SQLite schema 与连接（busy_timeout）
├── seed.py          # 演示数据（幂等）
├── verify.py        # 端到端校验：API 规则 + 页面渲染 + 并发（34 项断言）
├── verify_pages.js  # 页面渲染校验（DOM 桩加载真实 app.js）
└── static/          # 前端（index.html / app.js / styles.css，无构建步骤）
```

数据文件 `data.db` 首次启动自动生成。与同目录下既有项目 `sterile-packaging-release-control`（Go/React，检验放行维度）相互独立，可并行运行（端口不冲突）。
