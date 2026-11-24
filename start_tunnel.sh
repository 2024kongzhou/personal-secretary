#!/bin/bash
# 启动 cloudflared tunnel（已运行则跳过）
if ! pgrep -f 'cloudflared tunnel' > /dev/null 2>&1; then
  nohup /vol1/1000/personal-secretary/cloudflared tunnel     --config /home/kongzhou/.cloudflared/config.yml run     >> /vol1/1000/personal-secretary/tunnel.log 2>&1 &
  echo "[$(date)] cloudflared started PID=$!" >> /vol1/1000/personal-secretary/tunnel.log
fi
