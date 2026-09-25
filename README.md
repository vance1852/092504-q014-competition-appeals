# 竞赛申诉协作服务

本项目提供赛事组织机构、案件场所、承办人员与申诉资料的统一后台能力，包含两部分：

1. **基础协作服务**：机构、场所、操作者和领域资料的登记，支持请求幂等、角色权限、SQLite 事务与哈希串联审计。
2. **申诉证据封存与裁决服务**：按成绩版本和申诉期限受理申诉，对陈述、结构化日志摘要和证明文件只保存内容摘要（SHA-256）与存储引用，每次补充形成不可变版本；系统先检查资格、重复申请和利益冲突，再从合格人员中分派带租约的承办人；承办人可要求补证、建议维持/更正/驳回，最终决定须经独立复核并在同一事务内原子更新成绩引用；超时租约可回收且旧代号立即失效；撤诉、驳回与裁决均为受保护终态；公开结果隐藏个人信息，审计视图可还原材料时间线、回避过程和决定依据。

各项资料通过稳定业务键保存，相同请求会返回原回执，不同内容复用编号时返回明确冲突。

## 目录

- `src/skills_workspace/`
  - `domain.py` / `models.py` / `errors.py` / `clock.py`：领域常量、数据对象、业务异常与时钟；
  - `storage.py`：SQLite 连接、建表和事务边界；
  - `audit.py`：可离线校验的哈希串联审计链；
  - `requests.py`：跨服务复用的幂等请求回执；
  - `service.py`：基础登记服务（机构、操作者、场所、领域资料）；
  - `appeals.py`：申诉证据封存与裁决工作流；
  - `api.py`：不依赖第三方框架的 HTTP/JSON 边界；
  - `acceptance.py` / `appeal_acceptance.py`：两部分各自的离线端到端验收。
- `tests/`：核心规则、事务边界、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m skills_workspace.acceptance
PYTHONPATH=src python3 -m skills_workspace.appeal_acceptance
```

两条命令都在临时 SQLite 数据库中执行完整链路：前者核对基础登记的幂等回执与审计链；后者核对回避冲突检测、证据不可变版本、租约回收、独立复核、成绩原子更正与公开结果匿名化。成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m skills_workspace.api --database skills_workspace.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。所有业务写入接口通过 `X-Actor-Id` 标识操作者，写接口以 `request_id` 保证幂等。服务重启后，SQLite 中的业务状态和审计链继续保留。

### 申诉工作流接口

| 方法与路径 | 角色 | 说明 |
| --- | --- | --- |
| `POST /score-versions` | admin, operator | 按 `(场地, 赛项, 选手)` 公布新版本成绩与申诉期限 |
| `POST /disclosures` | admin, operator, reviewer | 登记裁决人员的回避披露（选手/赛项/组织） |
| `POST /conflict-checks` | admin, operator | 对候选承办/复核人做利益冲突核对 |
| `POST /appeals` | admin, operator, competitor | 资格（期限、当前版本、重复申请）通过后受理立案 |
| `POST /appeal-assignments` | admin, operator | 从回避合格人员中分派承办人并签发限时租约 |
| `POST /lease-renewals` | reviewer | 承办人持有效代号续租，旧代号失效 |
| `POST /lease-reclaims` | admin, operator | 回收所有超时租约，案件回到待分派 |
| `POST /materials` | admin, operator, competitor, reviewer | 封存证据版本；只存摘要、存储引用和元数据 |
| `GET  /materials?case_id=` | 当事人/承办/复核/秘书/审计 | 列出版本化证据 |
| `POST /supplement-requests` | reviewer（承办人） | 要求补证 |
| `POST /recommendations` | reviewer（承办人） | 建议 `uphold` / `correct` / `reject`，进入复核 |
| `POST /reviewer-assignments` | admin, operator | 从合格且非承办人的人员中指定独立复核人 |
| `POST /reviews` | reviewer（复核人） | 批准终局决定（原子更正成绩）或退回承办人 |
| `POST /withdrawals` | admin, operator, competitor | 撤诉（受保护终态） |
| `GET  /cases` | admin, operator, auditor | 按状态/场所列出案件 |
| `GET  /case?case_id=` | 当事人/承办/复核/秘书/审计 | 案件当前状态视图 |
| `GET  /case-timeline?case_id=` | admin, auditor | 还原材料、回避、租约与决定的完整时间线 |
| `GET  /public-result?public_token=` | 公开 | 匿名化的最终裁决结果（不含个人标识） |

### 关键规则

- **只存摘要与引用**：证据（陈述 `statement`、结构化日志摘要 `log_summary`、证明文件 `evidence_document`）不落原文，仅保存 `content_sha256`、`storage_ref`、`summary_text`、`content_type` 与 `byte_length`；同一 `material_id` 每次补充追加为新版本，历史行不可变。
- **先核对再分派**：案件只能分派给已完成利益冲突核对且 `conflicted=0` 的裁决人员；本人为当事人或存在已登记披露的人员会被客观规则拦截。
- **租约隔离**：承办动作（补证要求、建议、登记材料）必须携带未过期的租约代号；续租轮换代号；超时后由秘书组回收，旧承办人不能再提交。
- **独立复核**：承办建议不直接生效；复核人必须与承办人不同且自身通过回避核对。复核不通过时退回承办人并签发新租约。
- **原子更正**：复核批准“更正”时，在同一 SQLite 事务内插入下一成绩版本并把旧版本指向新版本，避免出现引用悬空。
- **受保护终态**：`withdrawn`、`rejected`、`decided` 之后任何材料、租约或状态变更都被拒绝。
- **匿名公开**：仅 `decided` 案件可凭 `public_token` 查询；返回值用不可逆的 `competitor_ref` 替代选手标识，且不含任何承办/复核人员标识。
- **可审计**：案件内部时间线（`case_events`）与全局哈希审计链（`audit_events`）双重记录；`GET /health` 与基础服务验收都会校验审计链完整性。
