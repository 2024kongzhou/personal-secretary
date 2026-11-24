#!/bin/bash
set -e
TS=$(date +%Y%m%d_%H%M%S)
BAK_DIR="/vol1/1000/personal-secretary/backups"
ZIP_FILE="${BAK_DIR}/backup_${TS}.zip"
LOG="/vol1/1000/personal-secretary/data/logs/backup.log"
mkdir -p "${BAK_DIR}" "$(dirname $LOG)"
# 打包数据（排除备份目录本身）
cd /vol1/1000/personal-secretary/data
zip -r "${ZIP_FILE}" . --exclude "./backups/*" -q
SIZE=$(du -sh "${ZIP_FILE}" | cut -f1)
echo "$(date -Iseconds) OK: 本地备份 backup_${TS}.zip [${SIZE}]" >> "${LOG}"
# 只保留最近5个
ls -t "${BAK_DIR}"/backup_*.zip 2>/dev/null | tail -n +6 | xargs rm -f
echo "备份完成: ${ZIP_FILE} [${SIZE}]"
