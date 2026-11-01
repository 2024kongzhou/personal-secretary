#!/usr/bin/env python3
"""Personal Secretary API v0.4 — Dynamic Config, Multimedia Upload, Audio Record, Full Search, Cloudflare & SenseNova"""
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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DATA_DIR = pathlib.Path(os.environ.get("SECRETARY_ROOT", "/data"))
WEB_DIR  = pathlib.Path(os.environ.get("SECRETARY_WEB",  "/web"))
CONFIG_FILE = DATA_DIR / "config.json"
MAX_BYTES = 100 * 1024 * 1024

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
        except Exception:
            pass
    return DEFAULT_CONFIG

def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

CONFIG = load_config()

def get_destination(route):
    cfg = load_config()
    # 如果 route 属于项目名，或者以 "project:" 开头
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
    
    # 构建多附件统一列表
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
        except Exception:
            pass

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

class Handler(BaseHTTPRequestHandler):
    server_version = "PersonalSecretary/0.4"

    def log_message(self, fmt, *args):
        return

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,X-Secretary-Route")
        self.end_headers()

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/logs":
            log_entries = []
            log_dir = DATA_DIR / "logs"
            if log_dir.exists():
                for lf in sorted(log_dir.glob("*.log"), reverse=True)[:5]:
                    try:
                        with open(lf, "r", encoding="utf-8", errors="ignore") as f:
                            lines = f.readlines()
                            for l in lines[-40:]:
                                log_entries.append(l.strip())
                    except Exception:
                        pass
            if not log_entries:
                # 读取 recent records 构造最近活动流水日志
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
                "version": "0.4", "capture": "L0",
                "ai_enabled": cfg.get("ai", {}).get("enabled", False)
            })

        if path == "/api/config":
            cfg = load_config()
            # 遮蔽 key 返回
            safe_cfg = json.loads(json.dumps(cfg))
            if safe_cfg.get("ai", {}).get("key"):
                k = safe_cfg["ai"]["key"]
                safe_cfg["ai"]["key_masked"] = k[:6] + "..." + k[-4:] if len(k) > 10 else "***"
            return self.send_json(200, {"ok": True, "config": safe_cfg})

        if path == "/api/recent":
            qs = parse_qs(parsed.query)
            limit = int(qs.get("limit", ["50"])[0])
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

        if path.startswith("/api/files/"):
            rel = path[len("/api/files/"):]
            fpath = DATA_DIR / rel
            if fpath.is_file() and fpath.resolve().is_relative_to(DATA_DIR.resolve()):
                data = fpath.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(str(fpath))[0] or "application/octet-stream")
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
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/polish":
            length = int(self.headers.get("Content-Length", "0"))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                text = (body.get("text") or "").strip()
                if not text:
                    return self.send_json(400, {"ok": False, "error": "文本不能为空"})
                
                # 规则预处理：剔除口吃、语气停顿词
                clean_text = re.sub(r"[啊呃嗯呀那个然后就是说]+", "", text)
                clean_text = re.sub(r"\s+", " ", clean_text).strip()
                
                # 智能结构化梳理
                # 1. 尝试识别重点或行动项
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
                
                # 智能正则匹配票据与记账信息
                amount_match = re.search(r"(?:金额|合计|总计|实付|消费|￥|¥|支付)?[:：\s]*([0-9]+(?:\.[0-9]{1,2})?)\s*(?:元|块)?", text)
                amount = amount_match.group(1) if amount_match else ""
                
                # 日期匹配
                date_match = re.search(r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)", text)
                date_str = date_match.group(1) if date_match else datetime.now().strftime("%Y-%m-%d")
                
                # 商家/商户匹配
                merchant_match = re.search(r"(?:收款方|商户|商家|店铺|公司|在)(?:名称)?[:：\s]*([\u4e00-\u9fa5A-Za-z0-9（）()]+?)(?:[，,\s\n]|$)", text)
                merchant = merchant_match.group(1) if merchant_match else "未指定商户"
                
                # 消费类型推断
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

        if path == "/api/logs":
            log_entries = []
            log_dir = DATA_DIR / "logs"
            if log_dir.exists():
                for lf in sorted(log_dir.glob("*.log"), reverse=True)[:5]:
                    try:
                        with open(lf, "r", encoding="utf-8", errors="ignore") as f:
                            lines = f.readlines()
                            for l in lines[-40:]:
                                log_entries.append(l.strip())
                    except Exception:
                        pass
            if not log_entries:
                # 读取 recent records 构造最近活动流水日志
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
            length = int(self.headers.get("Content-Length", "0"))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                action = body.get("action") # add, edit, delete
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

        path = urlparse(self.path).path

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
                    cfg["ai"].update(new_cfg["ai"])
                if "webdav" in new_cfg:
                    cfg["webdav"].update(new_cfg["webdav"])
                save_config(cfg)
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
                return self.send_json(200, {"ok": True, "deleted": m.group(1)})
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

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
        self.send_json(201, {"ok": True, "record": record})

DATA_DIR.mkdir(parents=True, exist_ok=True)
for p in CONFIG.get("paths", {}).values():
    (DATA_DIR / p).mkdir(parents=True, exist_ok=True)

port = int(os.environ.get("PORT", "8080"))
ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
