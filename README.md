# 竞赛申诉证据封存与裁决服务

本项目在赛事组织、场所、人员与领域资料登记的基础能力之上，提供竞赛成绩公布后的**申诉证据封存与裁决服务**。

## 申诉裁决能力

- **按成绩版本与申诉期限受理**：申诉绑定具体成绩版本，仅在该版本公布后的申诉期限内受理；先校验选手资格，再拦截同一事项的重复申请。
- **证据只存摘要与引用**：陈述书、结构化日志摘要、证明文件均只保存 SHA-256 内容摘要与存储引用（`storage_ref`），不保存原始内容。每次补交都生成新的、单调递增的不可变版本，旧版本永不覆盖。
- **受理前三重核对**：依次检查资格、重复申请、利益冲突；裁决人员须逐人完成回避核对（已申报冲突者不能被标记为无冲突），至少有一名无冲突合格人员后才能进入分派。
- **有租约的承办分派**：仅从通过回避核对、无回避关系、且未承办过本案的合格人员中分派；承办持有带到期时间的租约。租约超时可被回收，案件回到待分派并提升租约代号，旧承办人随即无法再提交任何材料或建议。
- **补证、建议与独立复核**：承办人可要求补证、建议维持（`uphold`）或更正（`correct`，须给出拟更正的成绩引用）；最终驳回或裁决必须由从未承办本案、无回避关系的独立复核人作出。
- **原子更新成绩引用**：更正裁决与成绩引用更新在同一 SQLite 事务内完成，并记录前后引用；裁决失败则成绩不变。
- **受保护终态**：撤诉（`withdrawn`）、驳回（`rejected`）、裁决（`decided`）均为终态，此后任何材料、建议或状态变更都被拒绝。
- **公开结果去标识化**：公开视图仅返回案号、事由、结论与是否更正，隐藏选手、承办人等一切个人信息，且终局决定作出前不可访问。
- **可还原的审计视图**：哈希串联审计链记录材料时间线、回避核对过程、租约分派/回收与决定依据摘要，可离线校验防篡改。

## 目录

- `src/skills_workspace/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
  - `appeal_service.py`：申诉受理、证据版本、回避核对、租约承办与独立复核裁决；
- `tests/`：核心规则、事务边界、接口路由、申诉裁决与端到端验收测试。

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
```

命令会在临时 SQLite 数据库中登记机构、操作者、场所和领域资料，并跑通一条「受理 → 回避核对 → 分派 → 补证 → 建议 → 独立复核裁决 → 成绩更正」的完整申诉链，核对幂等回执、证据不可变版本、终态保护、公开结果去标识化与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m skills_workspace.api --database skills_workspace.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。业务写入接口通过 `X-Actor-Id` 标识操作者。所有写接口都要求 `request_id` 以支持幂等重放（相同请求返回原回执，不同内容复用编号返回冲突）。

申诉裁决主要接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/competitions` `/competitors` `/score-versions` | 竞赛、选手、成绩版本（含申诉期限）登记 |
| POST | `/adjudicators` `/conflict-declarations` | 裁决人员登记与利益冲突申报 |
| POST | `/appeals` | 在资格、期限、重复、回避核对通过后受理申诉 |
| POST | `/appeals/{id}/evidence` · GET 同路径 | 补交不可变证据版本 / 列出全部版本 |
| POST | `/appeals/{id}/screenings/check` · `.../screening/complete` | 逐人回避核对 / 完成核对 |
| POST | `/appeals/{id}/assignment/assign` | 分派有租约的承办人 |
| POST | `/leases/reclaim` | 回收全部超时租约 |
| POST | `/appeals/{id}/evidence/requests` | 承办人要求补证 |
| POST | `/appeals/{id}/recommendation` | 承办人建议维持或更正 |
| POST | `/appeals/{id}/withdraw` `/reject` `/decision` | 撤诉 / 驳回 / 终局裁决（独立复核人） |
| GET | `/appeals/{id}` · `.../snapshot` · `.../timeline` | 案件 / 办案视图 / 审计时间线 |
| GET | `/public/appeals/{case_number}` | 去标识化公开结果 |

服务重启后，SQLite 中的业务状态、证据版本、租约与审计链继续保留。

