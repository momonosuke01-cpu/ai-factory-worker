# Data Processing Worker 01
# 役割: 仕事(JSON)を受け取り、軽い整形・重複除去などの処理をして結果を返す。
# 外部ライブラリは使わず、Python標準機能だけで動く。
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("SERVER_PORT") or os.environ.get("PORT") or "8080")
# 合言葉（共有トークン）。環境変数 FACTORY_TOKEN で渡す。
# 未設定のときは安全側に倒し、仕事の受け付けと結果の返却を止める。
TOKEN = os.environ.get("FACTORY_TOKEN", "")
BASE = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(BASE, "results")
LOG_FILE = os.path.join(BASE, "jobs_log.jsonl")
os.makedirs(RESULTS_DIR, exist_ok=True)


def clean_lines(lines):
    """空白を整え、空行と重複を取り除く（順番は保持）。"""
    seen = set()
    out = []
    for line in lines:
        s = str(line).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def process(job):
    job_type = job.get("type", "text_clean")
    payload = job.get("payload", {})
    if job_type == "text_clean":
        items_in = payload.get("lines", [])
        items_out = clean_lines(items_in)
    else:
        raise ValueError(f"unknown job type: {job_type}")
    return {
        "job_id": job.get("id"),
        "type": job_type,
        "items_in": len(items_in),
        "items_out": len(items_out),
        "removed": len(items_in) - len(items_out),
        "result": {"lines": items_out},
        "ai_cost_yen": 0,  # ローカル処理のみ。外部AIを使ったら実費を入れる
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[worker] " + (fmt % args), flush=True)

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        if not TOKEN:
            return False
        given = self.headers.get("X-Factory-Token") or ""
        return hmac.compare_digest(given, TOKEN)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok", "role": "worker",
                             "auth": bool(TOKEN)})
            return
        if self.path.startswith("/results/"):
            if not TOKEN:
                self._send(503, {"error": "FACTORY_TOKEN is not set"})
                return
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return
            job_id = self.path[len("/results/"):]
            path = os.path.join(RESULTS_DIR, f"{job_id}.json")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    self._send(200, json.load(f))
            else:
                self._send(404, {"error": "result not found"})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/jobs":
            self._send(404, {"error": "not found"})
            return
        if not TOKEN:
            self._send(503, {"error": "FACTORY_TOKEN is not set"})
            return
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            job = json.loads(self.rfile.read(length) or b"{}")
            started = time.time()
            result = process(job)
            result["seconds"] = round(time.time() - started, 3)
            with open(os.path.join(RESULTS_DIR, f"{result['job_id']}.json"), "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps({k: result[k] for k in
                                     ("job_id", "type", "items_in", "items_out",
                                      "removed", "ai_cost_yen", "seconds")},
                                    ensure_ascii=False) + "\n")
            print(f"[worker] job {result['job_id']} done: "
                  f"{result['items_in']} -> {result['items_out']}", flush=True)
            self._send(200, result)
        except Exception as e:
            self._send(400, {"error": str(e)})


if __name__ == "__main__":
    print(f"[worker] starting on port {PORT} (auth={'on' if TOKEN else 'OFF - jobs disabled'})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
