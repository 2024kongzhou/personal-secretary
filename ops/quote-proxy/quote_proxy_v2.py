#!/usr/bin/env python3
"""行情代理 v2 — 容灾采集(多源自动排序+降级)+缓存+后台预热。
/quote-api?token=..&mode=all|rt|spark|health
"""
import json, os, re, subprocess, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from fault_tolerant import Source, FaultTolerantFetcher

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
TOKEN = os.environ.get("QUOTE_TOKEN", "xyq_2026_kongzhou")
AIHUB = "http://127.0.0.1:8000"
CACHE_TTL = int(os.environ.get("QUOTE_CACHE_TTL", "30"))


def fetch(url, ref="https://finance.sina.com.cn"):
    r = urllib.request.Request(url, headers={**UA, "Referer": ref})
    raw = urllib.request.urlopen(r, timeout=8).read()
    try:
        return raw.decode("utf-8")
    except Exception:
        return raw.decode("gbk", "ignore")


def _pct(cur, prev):
    try:
        cur, prev = float(cur), float(prev)
        return round((cur - prev) / prev * 100, 2) if prev else 0
    except Exception:
        return 0


# ---------- 上证指数实时: 3源容灾 ----------
def sh_sina():
    m = re.search(r'hq_str_sh000001="([^"]*)"', fetch("https://hq.sinajs.cn/list=sh000001"))
    f = m.group(1).split(",")
    if len(f) > 3 and f[3]:
        return {"name": "上证指数", "price": f[3], "change_pct": _pct(f[3], f[2])}
    raise ValueError("sina empty")

def sh_tencent():
    m = re.search(r'v_sh000001="([^"]*)"', fetch("https://qt.gtimg.cn/q=sh000001"))
    f = m.group(1).split("~")
    if len(f) > 4 and f[3]:
        return {"name": "上证指数", "price": f[3], "change_pct": _pct(f[3], f[4])}
    raise ValueError("tencent empty")

def sh_eastmoney():
    p = subprocess.run(["curl", "-s", "-m", "6", "-A", UA["User-Agent"],
                        "-H", "Referer: https://quote.eastmoney.com/",
                        "https://push2.eastmoney.com/api/qt/stock/get?fltt=2&invt=2&fields=f43,f170,f57,f58&secid=1.000001"],
                       capture_output=True, timeout=10)
    d = json.loads(p.stdout.decode("utf-8", "ignore")).get("data") or {}
    if d.get("f43") not in (None, "-"):
        return {"name": d.get("f58", "上证指数"), "price": str(d["f43"]), "change_pct": d.get("f170")}
    raise ValueError("em empty")

sh_fetcher = FaultTolerantFetcher([
    Source("腾讯", sh_tencent),    # 实测最稳
    Source("新浪", sh_sina),
    Source("东财", sh_eastmoney),
], cache_ttl=CACHE_TTL)


# ---------- 期货/外盘(新浪主源) ----------
def futures_sina():
    raw = fetch("https://hq.sinajs.cn/list=hf_GC,hf_SI,hf_OIL,hf_CL")
    out = {}
    for m in re.finditer(r'hq_str_(\w+)="([^"]*)"', raw):
        k, f = m.group(1), m.group(2).split(",")
        if k == "hf_GC" and f[0]:
            out["COMEX黄金"] = {"price": f[0], "change_pct": _pct(f[0], f[8])}
        elif k == "hf_SI" and f[0]:
            out["COMEX白银"] = {"price": f[0], "change_pct": _pct(f[0], f[8])}
        elif k == "hf_OIL" and f[0]:
            out["布伦特原油"] = {"price": f[0], "change_pct": _pct(f[0], f[8])}
        elif k == "hf_CL" and f[0]:
            out["NYMEX原油"] = {"price": f[0], "change_pct": _pct(f[0], f[8])}
    if not out:
        raise ValueError("futures empty")
    return out

fut_fetcher = FaultTolerantFetcher([Source("新浪期货", futures_sina)], cache_ttl=CACHE_TTL)


# ---------- 分时走势(东财串行+重试) ----------
def em_spark(secid):
    url = ("https://push2his.eastmoney.com/api/qt/stock/trends2/sse"
           f"?fields1=f1,f2,f3&fields2=f51,f53&iscr=0&ndays=1&secid={secid}")
    for _ in range(3):
        p = subprocess.run(["curl", "-s", "-m", "8", "-A", UA["User-Agent"],
                            "-H", "Referer: https://quote.eastmoney.com/", url],
                           capture_output=True, timeout=12)
        m = re.search(r'"trends":\[(.*?)\]', p.stdout.decode("utf-8", "ignore"))
        if m:
            pts = []
            for item in re.findall(r'"([^"]+)"', m.group(1)):
                try:
                    pts.append(float(item.rsplit(",", 1)[-1]))
                except Exception:
                    pass
            if len(pts) >= 8:
                step = max(1, len(pts) // 28)
                s = pts[::step]
                if s[-1] != pts[-1]:
                    s.append(pts[-1])
                return s
        time.sleep(0.6)
    return []

def sina_spark(sym):
    raw = fetch("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
                f"?symbol={sym}&scale=5&ma=no&datalen=28")
    try:
        return [float(x["close"]) for x in json.loads(raw) if x.get("close")]
    except Exception:
        return []

_spark_cache = {"ts": 0, "data": None}

def get_sparks():
    now = time.time()
    if _spark_cache["data"] is not None and now - _spark_cache["ts"] < 120:
        return _spark_cache["data"]
    out = {"上证指数": sina_spark("sh000001"),
           "COMEX黄金": em_spark("101.GC00Y"),
           "布伦特原油": em_spark("112.B00Y")}
    out = {k: v for k, v in out.items() if len(v) >= 8}
    if out:
        _spark_cache.update(ts=now, data=out)
    return out


def build_rt():
    sh = sh_fetcher.get()
    fut = fut_fetcher.get()
    quotes = {"上证指数": {"price": sh["price"], "change_pct": sh["change_pct"], "ok": True}}
    for n in ("COMEX黄金", "COMEX白银", "布伦特原油", "NYMEX原油"):
        it = fut.get(n)
        quotes[n] = ({"price": it["price"], "change_pct": it["change_pct"], "ok": True}
                     if it else {"ok": False, "error": "获取失败"})
    return {"ok": True, "quotes": quotes,
            "_sh_source": sh.get("_source"), "_cached": bool(sh.get("_cached"))}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        if path == "/quote-api":
            if params.get("token") != TOKEN:
                return self._send(403, json.dumps({"ok": False, "error": "forbidden"}))
            mode = params.get("mode", "all")
            try:
                if mode == "health":
                    return self._send(200, json.dumps({"ok": True,
                        "sh_health": sh_fetcher.health(), "fut_health": fut_fetcher.health()}, ensure_ascii=False))
                res = {"ok": True}
                if mode in ("all", "rt"):
                    res.update(build_rt())
                if mode in ("all", "spark"):
                    res["sparks"] = get_sparks()
                self._send(200, json.dumps(res, ensure_ascii=False))
            except Exception as e:
                self._send(502, json.dumps({"ok": False, "error": str(e)}))
        else:
            try:
                r = urllib.request.Request(AIHUB + self.path, headers={"User-Agent": "quote-proxy"})
                resp = urllib.request.urlopen(r, timeout=15)
                body = resp.read()
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self._send(502, json.dumps({"ok": False, "error": str(e)}))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

if __name__ == "__main__":
    PORT = int(os.environ.get("QUOTE_PORT", "8010"))
    Server(("0.0.0.0", PORT), Handler).serve_forever()