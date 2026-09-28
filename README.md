# 生成内容合规审查

纯 Python 服务端基础项目，提供版本化状态、幂等命令、SQLite 持久化和 JSON API 边界。

## 批量导入语义

合规团队每天批量导入生成内容，不同负责人逐项确认：

- **坏数据不卡批**：逐条校验，合法条目直接进入 `pending` 待处理；失败条目收集到批次报告的 `failures`（含原始下标、失败原因、原始数据），整批不再被单条坏数据阻断。
- **批次幂等**：以 `batch_id` 为幂等键。同一批次再次送达时原样回放首次报告（`replayed: true`），不会重复创建条目；条目无显式 id 时，由 `batch_id + 下标 + 负责人 + 内容` 生成确定性 id。
- **批次 / 负责人关系**：每条记录带 `batch_id` 和 `owner`，可按批次、负责人、状态过滤查询。
- **进度查询**：`progress` 分为 `confirmed`（已确认/approved）、`pending`（待处理）、`needs_fix`（rejected 条目附驳回原因，以及导入失败条目附校验原因）三组及计数。驳回修正后可重新回到 `pending` 送审。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/batches` | 批量导入，body：`{batch_id, items:[{id?, content, owner}], actor?}`；首次 201，重复送达 200 回放 |
| GET | `/batches/{batch_id}` | 批次导入报告（含 accepted / failures） |
| GET | `/batches/{batch_id}/progress?owner=` | 批次进度三桶 |
| GET | `/progress?batch_id=&owner=` | 跨批次进度 |
| GET | `/cases?batch_id=&owner=&state=` | 条目明细过滤 |
| POST | `/cases/{id}/move` | 状态流转，body：`{state, actor, reason?}`（pending→approved/rejected） |
| POST | `/cases` | 单条创建（旧接口，支持 idempotency_key） |

## 持久化

`SQLiteStore` 维护 `items` / `batches` / `idem_keys` 三张表，进程重启后批次幂等与进度数据依然有效；旧 `snapshots` 表接口保留兼容。

测试命令：python3 -m unittest discover -s tests -v

编译命令：python3 -m compileall -q service_09261_011 tests
