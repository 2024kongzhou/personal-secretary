# 行情容灾代理(部署在甲骨文日本节点 138.2.54.176)

链路: 小雅容器 → docker网关(172.17.0.1:18766) → SSH隧道 → 甲骨文:8010 → 新浪/腾讯/东财

## 文件
- fault_tolerant.py  容灾引擎(健康度评分+自动排序+降级+冷却恢复)
- quote_proxy_v2.py  代理服务(生产跑的,systemd: quote-proxy)
- drill.py           容灾降级演练(验证主源挂掉自动切换)

## 容灾规则
- 每成功 score+10(上限100), 每失败 score-30, 连败>=3 进入60s冷却
- 按健康分排序,失败自动降级下一源,恢复后自动回升
- 全部失败返回最近可用旧数据(stale),绝不报错给前端

## 上证指数3采集点(按实测稳定度)
1. 腾讯 qt.gtimg.cn  (最稳,GBK)
2. 新浪 hq.sinajs.cn (稳,偶有抖动,GBK)
3. 东财 push2 (实时常限流,走势才稳定)

## 部署
systemd: quote-proxy.service (QUOTE_PORT=8010 QUOTE_TOKEN=xyq_2026_kongzhou)
NAS侧: quote-tunnel.service (SSH隧道 18766→8010)
