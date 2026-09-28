"""JSON API 适配器。"""
from urllib.parse import urlparse, parse_qs


def dispatch(flow, method, path, body=None, query=""):
    body = body if isinstance(body, dict) else {}
    try:
        parsed = urlparse(path)
        clean_path = parsed.path
        params = {k: v[0] for k, v in parse_qs(parsed.query or query).items()}

        # ---- 批量导入与批次查询 ----
        if method == "POST" and clean_path == "/batches":
            if "batch_id" not in body or "items" not in body:
                return 400, {"error": "missing batch_id or items"}
            report = flow.import_batch(body["batch_id"], body["items"],
                                       body.get("actor", "system"))
            # 重复送达回放已有报告，不产生新记录。
            return 200 if report.get("replayed") else 201, report

        parts = [p for p in clean_path.split("/") if p]
        if method == "GET" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "progress":
            return 200, flow.progress(batch_id=parts[1], owner=params.get("owner"))
        if method == "GET" and len(parts) == 2 and parts[0] == "batches":
            return 200, flow.get_batch(parts[1])

        if method == "GET" and clean_path == "/progress":
            return 200, flow.progress(batch_id=params.get("batch_id"),
                                      owner=params.get("owner"))

        # ---- 单条创建 / 状态流转（保留原接口）----
        if method == "POST" and clean_path == "/cases":
            if "id" not in body or "actor" not in body:
                return 400, {"error": "missing id or actor"}
            return 201, flow.create(body["id"], body["actor"],
                                    body.get("idempotency_key")).__dict__
        if method == "POST" and len(parts) == 3 and parts[0] == "cases" and parts[2] == "move":
            if "state" not in body or "actor" not in body:
                return 400, {"error": "missing state or actor"}
            return 200, flow.move(parts[1], body["state"], body["actor"],
                                  body.get("reason", "")).__dict__

        # ---- 条目查询，可按批次 / 负责人 / 状态过滤 ----
        if method == "GET" and clean_path == "/cases":
            return 200, flow.snapshot(batch_id=params.get("batch_id"),
                                      owner=params.get("owner"),
                                      state=params.get("state"))
        return 404, {"error": "not_found"}
    except KeyError as exc:
        return 404, {"error": "not_found", "id": exc.args[0] if exc.args else ""}
    except ValueError as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}
