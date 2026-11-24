#!/usr/bin/env python3
"""Personal Secretary API v0.5 — 安全加固版
- 添加 Bearer Token 认证
- 限制 CORS 为白名单
- 添加运行日志 (RotatingFileHandler)
- 添加数据导出接口
- 统一异常处理
"""
import json
import email
import mimetypes
import os
import pathlib
import re
import shutil
import uuid
import urllib.request
import urllib.error
import hmac
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import zipfile
import threading
import time

# ========== 配置 ==========
DATA_DIR = pathlib.Path(os.environ.get("SECRETARY_ROOT", "/data"))
WEB_DIR  = pathlib.Path(os.environ.get("SECRETARY_WEB",  "/web"))
CONFIG_FILE = DATA_DIR / "config.json"
MAX_BYTES = 100 * 1024 * 1024

# 认证令牌：优先从环境变量读取，否则使用默认（应尽快更换）
AUTH_TOKEN = os.environ.get("SECRETARY_TOKEN", "XiaoyaSecret2026!")
# CORS 白名单：仅允许本机/局域网访问
CORS_ORIGINS = {
    "http://192.168.1.100:18765",
    "http://localhost:18765",
    "http://127.0.0.1:18765",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
}

# 日志设置
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(
            LOG_DIR / "app.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8"
        )
    ]
)
logger = logging.getLogger("personal-secretary")

# ========== 默认配置 ==========
DEFAULT_CONFIG = {
    "project_list": [],
    "account_categories": ["日常餐饮", "差旅交通", "工程采购", "办公开销"],
    "paths": {
        "inbox": "inbox",
        "A工地": "projects/工地",
        "随想": "ideas",
        "记账": "accounting",
        "记账凭证": "accounting",
        "derived": "derived",
        "logs": "logs"
    },
    "ai": {
        "url": "https://token.sensenova.cn/v1",
        "key": os.environ.get("AI_API_KEY", ""),
        "model": "SenseChat-5",
        "enabled": True
    },
    "webdav": {
        "enabled": False,
        "url": "",
        "username": "",
        "password": "",
        "encrypt": True
    }
}

def load_config():
    if CONFIG_FILE.is_file():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            merged = DEFAULT_CONFIG.copy()
            merged.update(cfg)
            return merged
        except Exception as e:
            logger.error(f"加载配置失败: {e}")
    return DEFAULT_CONFIG

def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

CONFIG = load_config()

REMINDERS_FILE = DATA_DIR / "reminders.json"

def load_reminders():
    if REMINDERS_FILE.is_file():
        try:
            return json.loads(REMINDERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []

def save_reminders(items):
    REMINDERS_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

def get_destination(route):
    cfg = load_config()
    if route.startswith("project:"):
        proj_name = route.split(":", 1)[1]
        folder = DATA_DIR / "projects" / safe_name(proj_name)
    elif route in cfg.get("project_list", ["A工地"]):
        folder = DATA_DIR / "projects" / safe_name(route)
    else:
        sub = cfg.get("paths", {}).get(route, route)
        folder = DATA_DIR / sub
    folder.mkdir(parents=True, exist_ok=True)
    return folder

ROUTE_ALIASES = {
    "inbox": "inbox", "project": "A工地", "idea": "随想",
    "accounting": "记账", "receipt": "记账凭证",
}

def safe_name(value):
    value = pathlib.Path(value or "attachment").name
    value = re.sub(r"[^\w一-龥. -]+", "_", value, flags=re.UNICODE).strip(" .")
    return value[:120] or "attachment"

def now_stamp():
    return datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")

def write_record(route, text, filename=None, content_type=None, raw_path=None, files_list=None):
    folder = get_destination(route)
    stamp = now_stamp()
    record_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
    
    attached_files = []
    if files_list:
        for item in files_list:
            attached_files.append({
                "path": str(item["raw_path"].relative_to(DATA_DIR)) if item.get("raw_path") else None,
                "filename": item.get("filename"),
                "content_type": item.get("content_type")
            })
    elif raw_path:
        attached_files.append({
            "path": str(raw_path.relative_to(DATA_DIR)),
            "filename": filename,
            "content_type": content_type
        })

    primary_file = attached_files[0]["path"] if attached_files else None
    primary_name = attached_files[0]["filename"] if attached_files else None
    primary_type = attached_files[0]["content_type"] if attached_files else None

    record = {
        "id": record_id,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "route": route,
        "original_text": text or "",
        "original_filename": primary_name,
        "content_type": primary_type,
        "raw_file": primary_file,
        "files": attached_files,
        "processing": {"state": "captured", "level": "L0", "derived": False},
    }
    (folder / f"{record_id}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"记录创建: {record_id} route={route}")
    return record

def all_records():
    for p in DATA_DIR.glob("**/*.json"):
        if p == CONFIG_FILE:
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("id"):
                yield p, data
        except Exception:
            continue

def find_record(record_id):
    for path, record in all_records():
        if record.get("id") == record_id:
            return path, record
    return None, None

def load_recent_records(limit=25):
    """返回最近写入的记录列表（按 received_at 降序）。"""
    items = []
    for p, data in all_records():
        try:
            items.append(data)
        except Exception:
            continue
    items.sort(key=lambda r: r.get("received_at", ""), reverse=True)
    return items[:limit]

def analyze_with_rules(record):
    text   = record.get("original_text", "")
    route  = record.get("route", "inbox")
    labels = []
    evidence = []
    uncertainties = []
    if route == "inbox":
        if re.search(r"灵感|想法|点子|创意|突然想到|构思", text):
            labels.append({"destination": "随想", "confidence": "high", "reason": "包含个人随想或创意点子"})
            evidence.append("包含灵感表达")
        if re.search(r"工地|现场|施工|材料|进度|客户|工长|安全|图纸|砌筑", text):
            labels.append({"destination": "A工地", "confidence": "high", "reason": "属于工地现场及施工事务"})
            evidence.append("包含工地工程词汇")
        if re.search(r"元|￥|¥|发票|收据|付款|支付|报销|转账|买|花", text):
            labels.append({"destination": "记账", "confidence": "high", "reason": "涉及款项交易或收支凭证"})
            evidence.append("包含金额收支词汇")
        if re.search(r"提醒|待办|要做|记得|安排|截止|联系|购买", text):
            labels.append({"destination": "待办候选", "confidence": "medium", "reason": "包含后续行动提示"})
            uncertainties.append("不能仅凭关键词自动生成正式日历提醒")
    if not labels:
        labels.append({"destination": "inbox", "confidence": "low", "reason": "信息不充分，默认留存收件箱"})

    candidate_fields = {}
    if route in ("记账", "记账凭证", "inbox"):
        amount = re.search(r"(?:￥|¥)?\s*(\d+(?:\.\d{1,2})?)\s*元?", text)
        candidate_fields = {
            "amount": amount.group(1) if amount else None,
            "merchant": None,
            "direction": "expense" if re.search(r"支出|花费|付款|购买|消费|买|付", text) else "income",
            "payment_method": "微信" if "微信" in text else ("支付宝" if "支付宝" in text else ("银行卡" if "银行" in text else "现金")),
        }
        if amount:
            evidence.append(f"提取出金额: {amount.group(1)}元")
    return {
        "candidate_labels": labels,
        "candidate_fields": candidate_fields,
        "evidence": evidence,
        "uncertainties": uncertainties,
    }

def analyze(record):
    cfg = load_config()
    text = record.get("original_text", "")
    route = record.get("route", "inbox")
    ai_cfg = cfg.get("ai", {})
    ai_result = None
    model_used = "rules-v0"

    if ai_cfg.get("enabled") and ai_cfg.get("key") and text:
        try:
            prompt = f"分析此文本并输出严格JSON：candidate_labels[{{\"destination\",\"confidence\",\"reason\"}}], candidate_fields{{\"amount\",\"merchant\",\"direction\",\"payment_method\"}}, evidence[], uncertainties[]。分类只能从：A工地、随想、记账、inbox选。\n输入：{text}"
            payload = json.dumps({
                "model": ai_cfg.get("model", "SenseChat-5"),
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1
            }).encode()
            req = urllib.request.Request(
                f"{ai_cfg.get('url')}/chat/completions",
                data=payload,
                headers={"Authorization": f"Bearer {ai_cfg.get('key')}", "Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                res = json.loads(r.read().decode())
                content = res["choices"][0]["message"]["content"].strip()
                content = re.sub(r"^```(?:json)?\s*", "", content)
                content = re.sub(r"\s*```$", "", content)
                ai_result = json.loads(content)
                model_used = f"sensenova/{ai_cfg.get('model')}"
        except Exception as e:
            logger.warning(f"AI 分析失败，使用规则回退: {e}")

    if not ai_result:
        fallback = analyze_with_rules(record)
        labels = fallback["candidate_labels"]
        candidate_fields = fallback["candidate_fields"]
        evidence = fallback["evidence"]
        uncertainties = fallback["uncertainties"]
    else:
        labels = ai_result.get("candidate_labels", [])
        candidate_fields = ai_result.get("candidate_fields", {})
        evidence = ai_result.get("evidence", [])
        uncertainties = ai_result.get("uncertainties", [])

    record["analysis"] = {
        "state": "candidate",
        "level": "L1",
        "candidate_labels": labels,
        "candidate_fields": candidate_fields,
        "evidence": evidence,
        "uncertainties": uncertainties,
        "model": model_used,
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
    }
    record.setdefault("processing", {})["derived"] = True
    return record


# ========== 自动归类落盘 (auto-file) ==========
# 置信度阈值: high/medium 自动归档, low 留在 inbox 等人工确认
AUTO_FILE_CONFIDENCE = {"high", "medium"}

def auto_file_record(record, rpath=None):
    """对单条 inbox 记录做 AI 分析并自动归类落盘。
    返回 (状态, 目标route)。状态: filed=已自动归档, skipped=置信度低保留inbox, error=异常。
    rpath 为记录 JSON 文件当前路径（调用方传入，用于移动和写回）。
    """
    if record.get("route") != "inbox":
        return ("skipped", record.get("route"))
    # 已确认的不再动
    if record.get("review", {}).get("state") == "confirmed":
        return ("skipped", record.get("route"))

    # 确保 analysis 存在（优先复用已有分析，节省 AI 调用）
    analysis = record.get("analysis") or {}
    labels = analysis.get("candidate_labels") or []
    if not labels:
        record = analyze(record)
        labels = record.get("analysis", {}).get("candidate_labels") or []

    if not labels:
        return ("skipped", "inbox")

    # 取第一个高/中置信度的目标
    target = None
    for lab in labels:
        if lab.get("confidence") in AUTO_FILE_CONFIDENCE:
            dest = lab.get("destination", "")
            # 跳过"待办候选"等非落盘目标
            if dest and dest not in ("inbox", "待办候选"):
                target = ROUTE_ALIASES.get(dest, dest)
                break

    if not target:
        return ("skipped", "inbox")

    # 落盘到目标文件夹
    try:
        dest_folder = get_destination(target)
        record["route"] = target
        record["review"] = {
            "state": "auto_filed",
            "filed_at": datetime.now(timezone.utc).isoformat(),
            "destination": target,
            "method": "auto-file",
        }
        # 真正移动文件 + 写回元数据（仅当传入 rpath 时）
        if rpath is not None:
            new_path = dest_folder / rpath.name
            rpath.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            if rpath.resolve() != new_path.resolve():
                shutil.move(str(rpath), str(new_path))
        return ("filed", target)
    except Exception as e:
        logger.error(f"auto-file 落盘失败: {e}")
        return ("error", target)


def _bg_autofile_by_id(record_id):
    """后台线程：按 id 重新定位记录并自动归类落盘（收集后异步触发用）。"""
    try:
        rpath, rec = find_record(record_id)
        if not rec:
            return
        if rec.get("route") != "inbox":
            return
        status, target = auto_file_record(rec, rpath=rpath)
        logger.info(f"capture 自动归类[{record_id[:8]}]: {status} -> {target}")
    except Exception as e:
        logger.error(f"capture 自动归类异常[{record_id[:8]}]: {e}")


class Handler(BaseHTTPRequestHandler):
    server_version = "PersonalSecretary/0.5"

    def log_message(self, fmt, *args):
        return

    # ========== 认证 ==========
    def check_auth(self):
        """检查 Bearer Token 认证。健康检查和无API静态资源无需认证。"""
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            return True  # 静态资源无需认证
        if path == "/api/health":
            return True  # 健康检查无需认证
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            return hmac.compare_digest(token, AUTH_TOKEN)
        return False

    def require_auth(self):
        if not self.check_auth():
            self.send_json(401, {"ok": False, "error": "未认证，请提供有效的 Bearer Token"})
            return False
        return True

    # ========== CORS ==========
    def set_cors_headers(self):
        origin = self.headers.get("Origin", "")
        if origin in CORS_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type,X-Secretary-Route,Authorization")
            self.send_header("Access-Control-Max-Age", "86400")
        # 如果不在白名单，不设置 CORS 头，浏览器会阻止跨域

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.set_cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.set_cors_headers()
        self.end_headers()

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if not self.require_auth():
            return

        try:
            if path == "/api/logs":
                log_entries = []
                log_file = LOG_DIR / "app.log"
                if log_file.exists():
                    with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                        lines = f.readlines()
                        log_entries = [l.strip() for l in lines[-100:]]
                if not log_entries:
                    records = load_recent_records(limit=25)
                    for r in records:
                        t = r.get("received_at", "")[:19].replace("T", " ")
                        rt = r.get("route", "inbox")
                        txt = (r.get("original_text") or r.get("original_filename") or "多媒体数据")[:30]
                        log_entries.append(f"[{t}] [CAPTURE:{rt}] {txt} (ID: {r.get('id','')[:8]})")
                return self.send_json(200, {"ok": True, "logs": log_entries})

            if path == "/api/accounts":
                cfg = load_config()
                alist = cfg.get("account_categories", ["日常餐饮", "差旅交通", "工程采购", "办公开销"])
                return self.send_json(200, {"ok": True, "categories": alist})

            if path == "/api/projects":
                cfg = load_config()
                plist = cfg.get("project_list", ["A工地"])
                return self.send_json(200, {"ok": True, "projects": plist})

            if path == "/api/health":
                cfg = load_config()
                return self.send_json(200, {
                    "ok": True, "service": "personal-secretary",
                    "version": "0.5", "capture": "L0",
                    "ai_enabled": cfg.get("ai", {}).get("enabled", False)
                })

            if path == "/api/config":
                cfg = load_config()
                safe_cfg = json.loads(json.dumps(cfg))
                if safe_cfg.get("ai", {}).get("key"):
                    k = safe_cfg["ai"]["key"]
                    safe_cfg["ai"]["key_masked"] = k[:6] + "..." + k[-4:] if len(k) > 10 else "***"
                    safe_cfg["ai"]["key"] = ""  # 不返回完整 key
                return self.send_json(200, {"ok": True, "config": safe_cfg})

            if path == "/api/reminders":
                items = load_reminders()
                items.sort(key=lambda x: (x.get("done", False), x.get("due_time", "") or "9999"))
                return self.send_json(200, {"ok": True, "reminders": items})

            if path == "/api/recent":
                qs = parse_qs(parsed.query)
                limit = min(int(qs.get("limit", ["50"])[0]), 500)
                route_filter = qs.get("route", [None])[0]
                kw = qs.get("q", [""])[0].strip().lower()

                records = []
                for p, data in all_records():
                    if route_filter and data.get("route") != route_filter:
                        continue
                    if kw:
                        txt = (data.get("original_text") or "").lower()
                        fname = (data.get("original_filename") or "").lower()
                        if kw not in txt and kw not in fname:
                            continue
                    records.append(data)
                records.sort(key=lambda x: x.get("received_at", ""), reverse=True)
                return self.send_json(200, {"ok": True, "records": records[:limit], "total": len(records)})

            if path == "/api/export":
                # 导出全部数据为 JSON（含元数据，不含原始附件内容）
                records = list(all_records())
                exported = {
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                    "version": "0.5",
                    "config": load_config(),
                    "reminders": load_reminders(),
                    "records": [data for _, data in records],
                }
                return self.send_json(200, {"ok": True, "export": exported})
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



            if path.startswith("/api/files/"):
                rel = path[len("/api/files/"):]
                fpath = DATA_DIR / rel
                if fpath.is_file() and fpath.resolve().is_relative_to(DATA_DIR.resolve()):
                    data = fpath.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", mimetypes.guess_type(str(fpath))[0] or "application/octet-stream")
                    self.set_cors_headers()
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                return self.send_error(404)

            fpath = (path.lstrip("/") or "index.html")
            file = WEB_DIR / fpath
            if not file.is_file() or not file.resolve().is_relative_to(WEB_DIR.resolve()):
                return self.send_error(404)
            data = file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(str(file))[0] or "application/octet-stream")
            self.set_cors_headers()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        except Exception as e:
            logger.error(f"GET 异常: {path} - {e}")
            self.send_json(500, {"ok": False, "error": str(e)})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if not self.require_auth():
            return

        try:
            if path == "/api/auto-file":
                """批量自动归类：遍历 inbox 待归档记录，AI 判定高/中置信度的自动落盘。"""
                filed, skipped, errors = [], [], []
                for p, rec in list(all_records()):
                    if rec.get("route") != "inbox":
                        continue
                    if rec.get("review", {}).get("state") == "confirmed":
                        continue
                    try:
                        status, target = auto_file_record(rec, rpath=p)
                        rid = rec.get("id", p.stem)
                        if status == "filed":
                            filed.append({"id": rid, "destination": target})
                        elif status == "error":
                            errors.append({"id": rid, "destination": target})
                        else:
                            skipped.append(rid)
                    except Exception as e:
                        errors.append({"id": rec.get("id", p.stem), "error": str(e)})
                logger.info(f"auto-file: 归档{len(filed)} 跳过{len(skipped)} 错误{len(errors)}")
                return self.send_json(200, {
                    "ok": True,
                    "filed_count": len(filed),
                    "skipped_count": len(skipped),
                    "error_count": len(errors),
                    "filed": filed,
                    "errors": errors,
                })

            if path == "/api/reminders":
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    action = body.get("action", "add")
                    items = load_reminders()
                    
                    if action == "add":
                        content = (body.get("content") or "").strip()
                        if not content:
                            return self.send_json(400, {"ok": False, "error": "内容不能为空"})
                        new_item = {
                            "id": f"rem-{now_stamp()}-{uuid.uuid4().hex[:6]}",
                            "content": content,
                            "due_time": body.get("due_time", ""),
                            "category": body.get("category", "待办"),
                            "done": False,
                            "created_at": datetime.now(timezone.utc).isoformat()
                        }
                        items.insert(0, new_item)
                        save_reminders(items)
                        logger.info(f"提醒创建: {new_item['id']}")
                        return self.send_json(201, {"ok": True, "reminder": new_item})
                    
                    elif action == "toggle":
                        rem_id = body.get("id")
                        for it in items:
                            if it.get("id") == rem_id:
                                it["done"] = not it.get("done", False)
                                it["completed_at"] = datetime.now(timezone.utc).isoformat() if it["done"] else None
                                break
                        save_reminders(items)
                        return self.send_json(200, {"ok": True, "reminders": items})
                    
                    elif action == "delete":
                        rem_id = body.get("id")
                        items = [it for it in items if it.get("id") != rem_id]
                        save_reminders(items)
                        return self.send_json(200, {"ok": True, "reminders": items})
                    
                    return self.send_json(400, {"ok": False, "error": "未知 action"})
                except Exception as e:
                    return self.send_json(500, {"ok": False, "error": str(e)})

            if path == "/api/polish":
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    text = (body.get("text") or "").strip()
                    if not text:
                        return self.send_json(400, {"ok": False, "error": "文本不能为空"})
                    
                    clean_text = re.sub(r"[啊呃嗯呀那个然后就是说]+", "", text)
                    clean_text = re.sub(r"\s+", " ", clean_text).strip()
                    
                    summary = clean_text[:40] + ("..." if len(clean_text) > 40 else "")
                    action_items = []
                    for sentence in re.split(r"[，。！？；\n]", clean_text):
                        s = sentence.strip()
                        if s and any(k in s for k in ["记得", "安排", "通知", "确认", "购买", "报销", "联系", "提交", "验收"]):
                            action_items.append(s)
                    
                    polished = f"【要点摘要】\n{clean_text}\n"
                    if action_items:
                        polished += "\n【提取事项】\n" + "\n".join(f"- {item}" for item in action_items)
                    
                    return self.send_json(200, {
                        "ok": True,
                        "original": text,
                        "polished": polished,
                        "action_items": action_items
                    })
                except Exception as e:
                    return self.send_json(500, {"ok": False, "error": str(e)})

            if path == "/api/parse_receipt":
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    text = (body.get("text") or "").strip()
                    
                    amount_match = re.search(r"(?:金额|合计|总计|实付|消费|￥|¥|支付)?[:：\s]*([0-9]+(?:\.[0-9]{1,2})?)\s*(?:元|块)?", text)
                    amount = amount_match.group(1) if amount_match else ""
                    
                    date_match = re.search(r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)", text)
                    date_str = date_match.group(1) if date_match else datetime.now().strftime("%Y-%m-%d")
                    
                    merchant_match = re.search(r"(?:收款方|商户|商家|店铺|公司|在)(?:名称)?[:：\s]*([\u4e00-\u9fa5A-Za-z0-9（）()]+?)(?:[，,\s\n]|$)", text)
                    merchant = merchant_match.group(1) if merchant_match else "未指定商户"
                    
                    cat = "日常餐饮"
                    if any(k in text for k in ["加油", "打车", "高铁", "机票", "停车", "路桥", "交通"]):
                        cat = "差旅交通"
                    elif any(k in text for k in ["水泥", "钢筋", "五金", "砂石", "租赁", "材料", "工程"]):
                        cat = "工程采购"
                    elif any(k in text for k in ["打印", "办公", "电脑", "纸张", "耗材", "快递"]):
                        cat = "办公开销"
                    
                    return self.send_json(200, {
                        "ok": True,
                        "data": {
                            "amount": amount,
                            "date": date_str,
                            "merchant": merchant,
                            "category": cat,
                            "confidence": "high" if amount else "medium"
                        }
                    })
                except Exception as e:
                    return self.send_json(500, {"ok": False, "error": str(e)})

            if path == "/api/accounts":
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    action = body.get("action")
                    name = (body.get("name") or "").strip()
                    cfg = load_config()
                    alist = list(cfg.get("account_categories", ["日常餐饮", "差旅交通", "工程采购", "办公开销"]))

                    if action == "add":
                        if not name:
                            return self.send_json(400, {"ok": False, "error": "分类名称不能为空"})
                        if name in alist:
                            return self.send_json(400, {"ok": False, "error": "分类已存在"})
                        alist.append(name)
                    elif action == "delete":
                        if name in alist and name not in ("日常餐饮", "差旅交通", "工程采购", "办公开销"):
                            alist.remove(name)

                    cfg["account_categories"] = alist
                    save_config(cfg)
                    return self.send_json(200, {"ok": True, "categories": alist})
                except Exception as e:
                    return self.send_json(500, {"ok": False, "error": str(e)})

            if path == "/api/projects":
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    action = body.get("action")
                    name = (body.get("name") or "").strip()
                    old_name = (body.get("old_name") or "").strip()
                    cfg = load_config()
                    plist = list(cfg.get("project_list", ["A工地"]))

                    if action == "add":
                        if not name:
                            return self.send_json(400, {"ok": False, "error": "项目名称不能为空"})
                        if name in plist:
                            return self.send_json(400, {"ok": False, "error": "项目已存在"})
                        plist.append(name)
                        (DATA_DIR / "projects" / safe_name(name)).mkdir(parents=True, exist_ok=True)
                    elif action == "edit":
                        if not name or not old_name:
                            return self.send_json(400, {"ok": False, "error": "参数不完整"})
                        if old_name in plist:
                            idx = plist.index(old_name)
                            plist[idx] = name
                            old_p = DATA_DIR / "projects" / safe_name(old_name)
                            new_p = DATA_DIR / "projects" / safe_name(name)
                            if old_p.exists() and not new_p.exists():
                                old_p.rename(new_p)
                    elif action == "reorder":
                        new_plist = body.get("projects")
                        if isinstance(new_plist, list):
                            plist = new_plist
                    elif action == "delete":
                        if name in plist:
                            plist.remove(name)

                    cfg["project_list"] = plist
                    save_config(cfg)
                    return self.send_json(200, {"ok": True, "projects": plist})
                except Exception as e:
                    return self.send_json(500, {"ok": False, "error": str(e)})

            if path == "/api/config":
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    new_cfg = json.loads(self.rfile.read(length).decode("utf-8"))
                    cfg = load_config()
                    if "paths" in new_cfg:
                        cfg["paths"].update(new_cfg["paths"])
                        for p in cfg["paths"].values():
                            (DATA_DIR / p).mkdir(parents=True, exist_ok=True)
                    if "ai" in new_cfg:
                        if new_cfg["ai"].get("key"):
                            cfg["ai"]["key"] = new_cfg["ai"]["key"]
                        if new_cfg["ai"].get("url"):
                            cfg["ai"]["url"] = new_cfg["ai"]["url"]
                        if new_cfg["ai"].get("model"):
                            cfg["ai"]["model"] = new_cfg["ai"]["model"]
                        if "enabled" in new_cfg["ai"]:
                            cfg["ai"]["enabled"] = new_cfg["ai"]["enabled"]
                    if "webdav" in new_cfg:
                        cfg["webdav"].update(new_cfg["webdav"])
                    save_config(cfg)
                    logger.info("配置已更新")
                    return self.send_json(200, {"ok": True, "message": "配置已保存生效", "config": cfg})
                except Exception as e:
                    return self.send_json(400, {"ok": False, "error": str(e)})

            m = re.fullmatch(r"/api/records/([^/]+)/analyze", path)
            if m:
                rpath, record = find_record(m.group(1))
                if not record:
                    return self.send_json(404, {"ok": False, "error": "record not found"})
                record = analyze(record)
                rpath.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                return self.send_json(200, {"ok": True, "record": record})

            m = re.fullmatch(r"/api/records/([^/]+)/confirm", path)
            if m:
                rpath, record = find_record(m.group(1))
                if not record:
                    return self.send_json(404, {"ok": False, "error": "record not found"})
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                except Exception:
                    payload = {}
                target = ROUTE_ALIASES.get(payload.get("destination", ""), payload.get("destination", ""))
                new_folder = get_destination(target)
                new_path = new_folder / rpath.name
                record["route"] = target
                if payload.get("confirmed_fields"):
                    record.setdefault("analysis", {})["confirmed_fields"] = payload["confirmed_fields"]
                record["review"] = {
                    "state": "confirmed",
                    "confirmed_at": datetime.now(timezone.utc).isoformat(),
                    "destination": target,
                }
                rpath.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                if rpath != new_path:
                    shutil.move(str(rpath), str(new_path))
                return self.send_json(200, {"ok": True, "record": record})

            m = re.fullmatch(r"/api/records/([^/]+)/update", path)
            if m:
                rpath, record = find_record(m.group(1))
                if not record:
                    return self.send_json(404, {"ok": False, "error": "record not found"})
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                except Exception:
                    payload = {}
                
                if "text" in payload:
                    record["original_text"] = payload["text"]
                if "title" in payload:
                    record["title"] = payload["title"]
                if "route" in payload and payload["route"]:
                    new_route = payload["route"]
                    target = ROUTE_ALIASES.get(new_route, new_route)
                    new_folder = get_destination(target)
                    new_path = new_folder / rpath.name
                    record["route"] = target
                    if rpath != new_path:
                        shutil.move(str(rpath), str(new_path))
                        rpath = new_path
                
                record["updated_at"] = datetime.now(timezone.utc).isoformat()
                rpath.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                return self.send_json(200, {"ok": True, "record": record})

            m = re.fullmatch(r"/api/records/([^/]+)/delete", path)
            if m:
                rpath, record = find_record(m.group(1))
                if not record:
                    return self.send_json(404, {"ok": False, "error": "record not found"})
                try:
                    if record.get("raw_file"):
                        rf = DATA_DIR / record["raw_file"]
                        if rf.is_file():
                            rf.unlink()
                    if rpath.is_file():
                        rpath.unlink()
                    logger.info(f"记录删除: {m.group(1)}")
                    return self.send_json(200, {"ok": True, "deleted": m.group(1)})
                except Exception as e:
                    return self.send_json(500, {"ok": False, "error": str(e)})

            
            if path == "/api/backup":
                import base64
                cfg_data = load_config()
                wdav = cfg_data.get("webdav", {})
                if not wdav.get("enabled") or not wdav.get("url"):
                    return self.send_json(400, {"ok": False, "error": "WebDAV未配置，请先在设置中填写WebDAV地址"})
                def do_backup():
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    zip_path = DATA_DIR / "backups" / f"backup_{ts}.zip"
                    zip_path.parent.mkdir(parents=True, exist_ok=True)
                    log_path = DATA_DIR / "logs" / "backup.log"
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                            for root, dirs, files in os.walk(DATA_DIR):
                                dirs[:] = [d for d in dirs if d != 'backups']
                                for f in files:
                                    fp = pathlib.Path(root) / f
                                    zf.write(fp, fp.relative_to(DATA_DIR))
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
                        existing = log_path.read_text(errors='replace').splitlines() if log_path.exists() else []
                        log_path.write_text("\n".join((existing + [msg])[-20:]) + "\n")
                        baks = sorted(pathlib.Path(DATA_DIR / "backups").glob("backup_*.zip"))
                        for old in baks[:-5]:
                            old.unlink(missing_ok=True)
                    except Exception as e:
                        msg = f"{datetime.now().isoformat()} ERR: {e}"
                        existing = log_path.read_text(errors='replace').splitlines() if log_path.exists() else []
                        log_path.write_text("\n".join((existing + [msg])[-20:]) + "\n")
                        zip_path.unlink(missing_ok=True)
                t = threading.Thread(target=do_backup, daemon=True)
                t.start()
                return self.send_json(200, {"ok": True, "message": "备份任务已启动，请稍后查看状态"})

            if not path.startswith("/api/capture"):
                return self.send_error(404)

            length = int(self.headers.get("Content-Length", "0"))
            if length > MAX_BYTES:
                return self.send_json(413, {"ok": False, "error": "file too large"})

            ctype = self.headers.get("Content-Type", "")
            route = self.headers.get("X-Secretary-Route", "inbox").strip() or "inbox"
            route = ROUTE_ALIASES.get(route, route)

            text = ""
            files_list = []

            if "multipart/form-data" in ctype.lower():
                body = self.rfile.read(length)
                msg  = email.message_from_bytes(
                    (f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
                for part in msg.walk():
                    if part.is_multipart():
                        continue
                    disp = part.get("Content-Disposition", "")
                    field_m = re.search(r'name="([^"]+)"', disp)
                    if not field_m:
                        continue
                    field = field_m.group(1)
                    file_m = re.search(r'filename="([^"]*)"', disp)
                    value = part.get_payload(decode=True) or b""

                    if field == "route":
                        c = value.decode("utf-8", "replace").strip()
                        route = ROUTE_ALIASES.get(c, c) or route
                        continue
                    if file_m and file_m.group(1):
                        fname = safe_name(file_m.group(1))
                        ftype = part.get_content_type()
                        folder = get_destination(route)
                        r_path = folder / f"{now_stamp()}-{uuid.uuid4().hex[:8]}-{fname}"
                        r_path.write_bytes(value)
                        files_list.append({
                            "raw_path": r_path,
                            "filename": fname,
                            "content_type": ftype
                        })
                    elif field == "text":
                        text = value.decode(part.get_content_charset() or "utf-8", "replace")
            else:
                body = self.rfile.read(length)
                try:
                    data = json.loads(body.decode("utf-8"))
                    text = str(data.get("text", ""))
                    route = ROUTE_ALIASES.get(data.get("route", route), data.get("route", route))
                except Exception:
                    text = body.decode("utf-8", "replace")

            record = write_record(route, text, files_list=files_list)
            # 收集箱内容：异步触发 AI 自动归类落盘（不阻塞前端响应）
            if record.get("route") == "inbox":
                threading.Thread(
                    target=_bg_autofile_by_id,
                    args=(record.get("id"),),
                    daemon=True,
                ).start()
            self.send_json(201, {"ok": True, "record": record})

        except Exception as e:
            logger.error(f"POST 异常: {path} - {e}")
            self.send_json(500, {"ok": False, "error": str(e)})

# ========== 启动 ==========
DATA_DIR.mkdir(parents=True, exist_ok=True)
for p in CONFIG.get("paths", {}).values():
    (DATA_DIR / p).mkdir(parents=True, exist_ok=True)

port = int(os.environ.get("PORT", "8080"))
logger.info(f"服务启动，端口: {port}，认证: {'启用' if AUTH_TOKEN else '禁用'}")
ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
