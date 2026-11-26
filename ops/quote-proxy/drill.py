#!/usr/bin/env python3
"""容灾降级演练 — 模拟主源挂掉,验证自动切换。不动生产。"""
import sys, time, re, urllib.request
sys.path.insert(0, "/home/ubuntu/quote-proxy")
from fault_tolerant import Source, FaultTolerantFetcher

def ok_tencent():
    raw = urllib.request.urlopen(urllib.request.Request(
        "https://qt.gtimg.cn/q=sh000001", headers={"User-Agent": "Mozilla/5.0"}), timeout=8).read()
    text = raw.decode("gbk", "ignore")  # 腾讯是GBK
    m = re.search(r'v_sh000001="([^"]*)"', text)
    f = m.group(1).split("~")
    return {"price": f[3], "src": "腾讯"}

def broken_sina():   # 模拟新浪挂
    raise RuntimeError("新浪 503")

def broken_em():     # 模拟东财挂
    raise RuntimeError("东财限流")

f = FaultTolerantFetcher([
    Source("新浪", broken_sina),
    Source("东财", broken_em),
    Source("腾讯", ok_tencent),
], cache_ttl=0, enable_refresher=False)  # 关预热,隔离测降级,避免并发限流

print("=== 演练: 新浪+东财都挂,应自动切腾讯 ===")
for i in range(3):
    d = f.get()
    print(f"  第{i+1}次: price={d['price']} 采集源={d.get('_source')} (期望=腾讯)")

print("=== 各源健康度(挂的应掉分) ===")
for h in f.health():
    print(f"  {h['name']}: 分{h['score']} 连败{h['consec_fail']} 可用={h['available']}")