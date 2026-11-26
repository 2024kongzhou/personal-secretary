#!/usr/bin/env python3
"""容灾采集引擎 — 多采集点按健康度自动排序,失败降级,恢复回升。
健康度规则:
  - 每次成功: score += 10 (上限100), 连续失败清零
  - 每次失败: score -= 30, 连续失败+1
  - 连续失败>=3 进入冷却(60s内不再首选)
  - 延迟作为次级排序(同分时快的优先)
"""
import time, threading

class Source:
    def __init__(self, name, fetch_fn):
        self.name = name
        self.fetch = fetch_fn      # () -> data 或抛异常
        self.score = 100.0         # 健康分 0~100
        self.consec_fail = 0
        self.last_latency = 0.0
        self.cooldown_until = 0
        self.last_ok_ts = 0
        self.lock = threading.Lock()

    def try_fetch(self, timeout_budget):
        """尝试采集,返回 (ok, data, latency)。更新健康度。"""
        t0 = time.time()
        try:
            data = self.fetch()
            lat = time.time() - t0
            with self.lock:
                self.last_latency = lat
                self.consec_fail = 0
                self.score = min(100.0, self.score + 10)
                self.last_ok_ts = time.time()
                self.cooldown_until = 0
            return True, data, lat
        except Exception:
            lat = time.time() - t0
            with self.lock:
                self.consec_fail += 1
                self.score = max(0.0, self.score - 30)
                if self.consec_fail >= 3:
                    self.cooldown_until = time.time() + 60
            return False, None, lat

    def available(self):
        return time.time() >= self.cooldown_until

    def rank_key(self):
        # 可用优先,再按分数降序,再按延迟升序
        return (0 if self.available() else 1, -self.score, self.last_latency)


class FaultTolerantFetcher:
    def __init__(self, sources, cache_ttl=30, enable_refresher=True):
        self.sources = sources          # [Source,...]
        self.cache_ttl = cache_ttl
        self._cache = {"ts": 0, "data": None}
        self._cache_lock = threading.Lock()
        # 后台刷新线程(定时预热缓存,避免用户请求撞冷启动)
        self._stop = False
        if enable_refresher:
            threading.Thread(target=self._refresher, daemon=True).start()

    def _sorted_sources(self):
        return sorted(self.sources, key=lambda s: s.rank_key())

    def get(self, force=False):
        """取数据:缓存优先(容灾降级时可用旧数据),否则按健康度逐源尝试。"""
        now = time.time()
        with self._cache_lock:
            if not force and self._cache["data"] is not None \
               and now - self._cache["ts"] < self.cache_ttl:
                return {**self._cache["data"], "_cached": True}

        last_err = None
        for src in self._sorted_sources():
            ok, data, lat = src.try_fetch(self.cache_ttl)
            if ok and data:
                with self._cache_lock:
                    self._cache = {"ts": now, "data": data}
                return {**data, "_source": src.name, "_latency": round(lat, 3)}
            last_err = src.name
        # 全部失败 → 降级返回最近一次可用旧数据(stale)
        with self._cache_lock:
            if self._cache["data"] is not None:
                return {**self._cache["data"], "_stale": True,
                        "_age": round(now - self._cache["ts"], 1)}
        raise RuntimeError(f"所有采集点失败(last={last_err})")

    def health(self):
        """返回各采集点健康度快照(供监控/排序可视化)。"""
        return [{
            "name": s.name, "score": round(s.score, 1),
            "consec_fail": s.consec_fail, "latency": round(s.last_latency, 3),
            "available": s.available(), "cooldown": max(0, round(s.cooldown_until - time.time(), 1)),
            "rank": i + 1,
        } for i, s in enumerate(self._sorted_sources())]

    def _refresher(self):
        """后台定时刷新缓存,保证随时有热数据。"""
        while not self._stop:
            try:
                self.get(force=True)
            except Exception:
                pass
            time.sleep(min(self.cache_ttl, 30))