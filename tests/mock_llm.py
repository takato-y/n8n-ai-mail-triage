"""テスト用の偽 LLM サーバー。実際の LLM の気まぐれに頼らず、失敗時の分岐を確かめるために使う。

- /v1/chat/completions      … OpenAI 互換の形式で、わざと JSON でない文章を返す（TC-007）
- /fail/v1/chat/completions … 常に 503 を返す（再試行の回数を数える・TC-006）
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REQUESTS = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        REQUESTS.append(body)
        if self.path.startswith("/fail/"):
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        reply = {
            "id": "mock", "object": "chat.completion", "model": body.get("model", "mock"),
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": "承知しました。こちらは見積依頼のメールだと思います。"}}],
        }
        data = json.dumps(reply, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def start(port):
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
