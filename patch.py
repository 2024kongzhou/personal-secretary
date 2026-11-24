#!/usr/bin/env python3
"""给 server.py 打补丁：添加WebDAV备份接口 + import zipfile + 修复import"""
import re

src = open('/vol1/1000/personal-secretary/server.py', 'r').read()

# 1. 在 import 区添加 zipfile, threading, time
old_import = "from urllib.parse import urlparse, parse_qs"
new_import = "from urllib.parse import urlparse, parse_qs\nimport zipfile\nimport threading\nimport time"
if 'import zipfile' not in src:
    src = src.replace(old_import, new_import, 1)
    print("✓ 添加 zipfile/threading/time import")
else:
    print("- zipfile已存在，跳过")

# 2. 在 /api/export 接口之后插入 /api/backup GET（查询备份状态）
backup_get = '''
            if path == "/api/backup":
                cfg = load_config()
                wdav = cfg.get("webdav", {})
                backup_log = DATA_DIR / "logs" / "backup.log"
                last_line = ""
                if backup_log.exists():
                    lines = backup_log.read_text(errors='replace').strip().splitlines()
                    last_line = lines[-1] if lines else ""
                return self.send_json(200, {
                    "ok": True,
                    "webdav_enabled": wdav.get("enabled", False),
                    "webdav_url": wdav.get("url", ""),
                    "last_backup": last_line,
                })

'''

old_export_end = '                return self.send_json(200, {"ok": True, "export": exported})'
if '/api/backup' not in src and old_export_end in src:
    src = src.replace(old_export_end, old_export_end + backup_get, 1)
    print("✓ 添加 GET /api/backup")
else:
    print("- GET /api/backup 已存在或未找到插入点，跳过")

# 3. 在 do_POST 的 /api/config 之后插入 POST /api/backup（触发备份）
backup_post = '''
            if path == "/api/backup":
                cfg = load_config()
                wdav = cfg.get("webdav", {})
                if not wdav.get("enabled") or not wdav.get("url"):
                    return self.send_json(400, {"ok": False, "error": "WebDAV未配置，请先在设置中填写WebDAV地址"})
                # 异步执行备份
                def do_backup():
                    import zipfile, urllib.request, urllib.error, base64
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    zip_path = DATA_DIR / "backups" / f"backup_{ts}.zip"
                    zip_path.parent.mkdir(parents=True, exist_ok=True)
                    log_path = DATA_DIR / "logs" / "backup.log"
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                            for root, dirs, files in os.walk(DATA_DIR):
                                # 跳过备份目录本身，避免递归
                                dirs[:] = [d for d in dirs if d != 'backups']
                                for f in files:
                                    fp = pathlib.Path(root) / f
                                    zf.write(fp, fp.relative_to(DATA_DIR))
                        # 上传到WebDAV
                        wdav_url = wdav.get("url", "").rstrip("/")
                        fname = f"secretary_backup_{ts}.zip"
                        upload_url = f"{wdav_url}/{fname}"
                        req = urllib.request.Request(upload_url, method="PUT")
                        if wdav.get("username"):
                            cred = base64.b64encode(f"{wdav['username']}:{wdav.get('password','')}".encode()).decode()
                            req.add_header("Authorization", f"Basic {cred}")
                        req.add_header("Content-Type", "application/zip")
                        with open(zip_path, 'rb') as fz:
                            data_bytes = fz.read()
                        req.add_header("Content-Length", str(len(data_bytes)))
                        resp = urllib.request.urlopen(req, data=data_bytes, timeout=120)
                        msg = f"{datetime.now().isoformat()} OK: 上传成功 {fname} ({len(data_bytes)//1024}KB) HTTP {resp.status}"
                        # 只保留最近20条日志
                        existing = log_path.read_text(errors='replace').splitlines() if log_path.exists() else []
                        log_path.write_text("\\n".join((existing + [msg])[-20:]) + "\\n")
                        # 本地只保留最近5个备份
                        baks = sorted(pathlib.Path(DATA_DIR / "backups").glob("backup_*.zip"))
                        for old in baks[:-5]:
                            old.unlink(missing_ok=True)
                    except Exception as e:
                        msg = f"{datetime.now().isoformat()} ERR: {e}"
                        existing = log_path.read_text(errors='replace').splitlines() if log_path.exists() else []
                        log_path.write_text("\\n".join((existing + [msg])[-20:]) + "\\n")
                        # 失败时删除不完整的zip
                        zip_path.unlink(missing_ok=True)
                t = threading.Thread(target=do_backup, daemon=True)
                t.start()
                return self.send_json(200, {"ok": True, "message": "备份任务已启动，请稍后查看状态"})

'''

# 找到POST /api/config的结束位置插入
old_post_anchor = 'if path == "/api/capture"'
if '/api/backup' not in src and old_post_anchor in src:
    src = src.replace(old_post_anchor, backup_post + "            " + old_post_anchor, 1)
    print("✓ 添加 POST /api/backup")
else:
    print("- POST /api/backup 已存在或未找到插入点，跳过")

# 写回
open('/vol1/1000/personal-secretary/server.py', 'w').write(src)
print("✓ server.py 写入完成")

# 语法检查
import subprocess
result = subprocess.run(['python3', '-m', 'py_compile', '/vol1/1000/personal-secretary/server.py'], capture_output=True, text=True)
if result.returncode == 0:
    print("✓ 语法检查通过")
else:
    print(f"✗ 语法错误: {result.stderr}")
