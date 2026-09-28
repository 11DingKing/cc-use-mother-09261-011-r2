"""JSON API 适配器。"""


def dispatch(flow, method, path, body=None, query=None):
    body = body or {}
    try:
        if method == "POST" and path == "/cases":
            return 201, flow.create(body["id"], body["actor"],
                                    body.get("idempotency_key")).__dict__
        if method == "POST" and path.endswith("/move"):
            return 200, flow.move(path.split("/")[2], body["state"],
                                  body["actor"]).__dict__
        if method == "GET" and path == "/cases":
            return 200, flow.snapshot()

        # 批量导入：合法条目入库，非法条目逐条返回原因；批次 key 幂等
        if method == "POST" and path == "/batches":
            return 200, flow.import_batch(body["batch"], body.get("items", []))

        # 进度查询：/?batch=xxx 可限定批次，返回三态分组
        if method == "GET" and path == "/progress":
            batch = (query or {}).get("batch")
            return 200, flow.progress(batch)

        # 负责人逐项处理
        parts = path.strip("/").split("/")
        if method == "POST" and len(parts) == 3 and parts[0] == "cases":
            cid, action = parts[1], parts[2]
            if action == "confirm":
                return 200, flow.confirm(cid, body["actor"]).__dict__
            if action == "request-fix":
                return 200, flow.request_fix(cid, body["actor"],
                                             body.get("reason")).__dict__
            if action == "resubmit":
                return 200, flow.resubmit(cid, body["actor"]).__dict__
        return 404, {"error": "not_found"}
    except KeyError as exc:
        return 404, {"error": "not_found", "detail": str(exc)}
    except ValueError as exc:
        return 400, {"error": "invalid_request", "detail": str(exc)}
