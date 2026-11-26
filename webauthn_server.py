"""WebAuthn / Passkey 服务端验证（手写，仅依赖 cryptography + cbor2）

单用户系统：所有 Passkey 归属同一虚拟用户 xiaoya-owner。
- 注册：生成挑战 → 前端 navigator.credentials.create() → 验证 attestation → 存公钥
- 登录：生成挑战 → 前端 navigator.credentials.get()（人脸/指纹）→ 验证 assertion 签名

凭据持久化到 data/passkeys.json。挑战短期存内存（防重放）。
"""
import base64
import json
import os
import time
import hashlib
import logging
import threading

import cbor2
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.exceptions import InvalidSignature

logger = logging.getLogger("webauthn")

USER_HANDLE = b"xiaoya-owner"  # 单用户固定 handle
CHALLENGE_TTL = 180  # 挑战有效期 180 秒
SUPPORTED_ALGS = [-7, -257]  # ES256, RS256

# 挑战与注册会话存内存（重启失效，用户重新绑定即可，可接受）
_challenges = {}  # key -> {"challenge": bytes, "type": "registration"|"authentication", "ts": float, "rp_id": str, "username": str}
_lock = threading.Lock()

PASSKEYS_FILE = os.path.join(os.environ.get("SECRETARY_ROOT", "/data"), "passkeys.json")


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _load_passkeys():
    if not os.path.exists(PASSKEYS_FILE):
        return []
    try:
        with open(PASSKEYS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"读取 passkeys 失败: {e}")
        return []


def _save_passkeys(keys):
    os.makedirs(os.path.dirname(PASSKEYS_FILE), exist_ok=True)
    with open(PASSKEYS_FILE, "w", encoding="utf-8") as f:
        json.dump(keys, f, ensure_ascii=False, indent=2)


def _put_challenge(key, obj):
    with _lock:
        # 清理过期挑战
        now = time.time()
        for k in list(_challenges.keys()):
            if now - _challenges[k]["ts"] > CHALLENGE_TTL:
                del _challenges[k]
        _challenges[key] = obj


def _pop_challenge(key):
    with _lock:
        return _challenges.pop(key, None)


def _parse_authenticator_data(data: bytes):
    """解析 authenticatorData：rpIdHash(32) + flags(1) + signCount(4) + [attestedCredData] + [extensions]"""
    if len(data) < 37:
        raise ValueError("authenticatorData 太短")
    rp_id_hash = data[0:32]
    flags = data[32]
    sign_count = int.from_bytes(data[33:37], "big")
    idx = 37
    cred_data = None
    if flags & 0x40:  # AT: attested credential data present
        aaguid = data[idx:idx + 16]
        idx += 16
        cred_id_len = int.from_bytes(data[idx:idx + 2], "big")
        idx += 2
        cred_id = data[idx:idx + cred_id_len]
        idx += cred_id_len
        # COSE 公钥：cbor2 的 CBORDecoder 需 BytesIO 且此版本无 bytes_read，
        # 改用「解析首字节得到 item 长度」后切片，兼容有/无 extensions。
        cose_len = _cbor_item_len(data[idx:])
        cose_key = cbor2.loads(data[idx:idx + cose_len])
        idx += cose_len
        cred_data = {"aaguid": aaguid.hex(), "credential_id": cred_id, "cose_key": cose_key}
    return {"rp_id_hash": rp_id_hash, "flags": flags, "sign_count": sign_count, "cred_data": cred_data}


def _cbor_item_len(buf: bytes) -> int:
    """返回 buffer 中第一个 CBOR item 占用的字节数（通过解析头部 additional info 计算长度）"""
    if not buf:
        return 0
    b0 = buf[0]
    major = b0 >> 5
    ai = b0 & 0x1F
    if ai < 24:
        hdr = 1
        arg = ai
    elif ai == 24:
        hdr, arg = 2, buf[1]
    elif ai == 25:
        hdr, arg = 3, int.from_bytes(buf[1:3], "big")
    elif ai == 26:
        hdr, arg = 5, int.from_bytes(buf[1:5], "big")
    elif ai == 27:
        hdr, arg = 9, int.from_bytes(buf[1:9], "big")
    else:
        return len(buf)  # indefinite 等，兜底
    if major == 5:  # map: arg 个键值对，逐项累加长度
        pos = hdr
        for _ in range(arg):
            kl = _cbor_item_len(buf[pos:])
            pos += kl
            vl = _cbor_item_len(buf[pos:])
            pos += vl
        return pos
    if major == 4:  # array
        pos = hdr
        for _ in range(arg):
            pos += _cbor_item_len(buf[pos:])
        return pos
    if major in (0, 1, 2, 3, 6):  # int/bytes/text/tag：头部 + arg 字节内容
        content = arg if major in (2, 3, 6) else 0
        return hdr + content
    return hdr


# ---- COSE 公钥 → cryptography 公钥对象 ----
def _cose_to_pubkey(cose: dict):
    kty = cose.get(1)
    alg = cose.get(3)
    if kty == 2:  # EC2
        crv = cose.get(-1)
        x = cose.get(-2)
        y = cose.get(-3)
        if crv == 1:  # P-256
            pub_numbers = ec.EllipticCurvePublicNumbers(
                x=int.from_bytes(x, "big"),
                y=int.from_bytes(y, "big"),
                curve=ec.SECP256R1(),
            )
            return pub_numbers.public_key()
        raise ValueError(f"不支持的 EC 曲线: {crv}")
    elif kty == 3:  # RSA
        n = cose.get(-1)
        e = cose.get(-2)
        pub_numbers = rsa.RSAPublicNumbers(
            e=int.from_bytes(e, "big"),
            n=int.from_bytes(n, "big"),
        )
        return pub_numbers.public_key()
    raise ValueError(f"不支持的 COSE key 类型: {kty}")


def _verify_signature(pubkey, signature: bytes, signed_data: bytes, alg: int):
    if isinstance(pubkey, ec.EllipticCurvePublicKey):
        # ES256: DER-encoded ECDSA signature over SHA-256
        pubkey.verify(signature, signed_data, ec.ECDSA(hashes.SHA256()))
    elif isinstance(pubkey, rsa.RSAPublicKey):
        # RS256: PKCS1v15 over SHA-256
        pubkey.verify(signature, signed_data, __import__("cryptography").hazmat.primitives.asymmetric.padding.PKCS1v15(), hashes.SHA256())
    else:
        raise ValueError("未知公钥类型")


# ============ 对外 API ============
def begin_registration(rp_id: str, username: str, existing_cred_ids):
    """生成注册选项。返回 (options_dict, session_key)。"""
    challenge = os.urandom(32)
    session_key = _b64u(os.urandom(16))
    _put_challenge(session_key, {"challenge": challenge, "type": "registration",
                                 "ts": time.time(), "rp_id": rp_id, "username": username})
    exclude = [{"type": "public-key", "id": cid} for cid in (existing_cred_ids or [])]
    options = {
        "challenge": _b64u(challenge),
        "rp": {"name": "小雅全能助手", "id": rp_id},
        "user": {
            "id": _b64u(USER_HANDLE),
            "name": username or "xiaoya",
            "displayName": "小雅主人",
        },
        "pubKeyCredParams": [
            {"type": "public-key", "alg": -7},    # ES256
            {"type": "public-key", "alg": -257},  # RS256
        ],
        "timeout": CHALLENGE_TTL * 1000,
        "attestation": "none",
        "excludeCredentials": exclude,
        "authenticatorSelection": {
            "authenticatorAttachment": "platform",  # 平台认证器（人脸/指纹）
            "userVerification": "required",
            "residentKey": "preferred",
        },
    }
    return options, session_key


def finish_registration(session_key: str, client_data_json_b64u: str, attestation_object_b64u: str):
    """验证注册响应，返回待存的凭据 dict。抛异常表示失败。"""
    sess = _pop_challenge(session_key)
    if not sess or sess["type"] != "registration":
        raise ValueError("注册会话无效或已过期")

    client_data = json.loads(_b64u_decode(client_data_json_b64u))
    if client_data.get("type") != "webauthn.create":
        raise ValueError("clientData type 错误")
    if _b64u_decode(client_data.get("challenge", "")) != sess["challenge"]:
        raise ValueError("challenge 不匹配")

    att_obj = cbor2.loads(_b64u_decode(attestation_object_b64u))
    fmt = att_obj.get("fmt")
    auth_data_raw = att_obj.get("authData")
    att_stmt = att_obj.get("attStmt", {})

    auth_data = _parse_authenticator_data(auth_data_raw)
    if not auth_data["flags"] & 0x40 or not auth_data["cred_data"]:
        raise ValueError("缺少 attested credential data")

    # 验证 rpIdHash
    expected_hash = hashlib.sha256(sess["rp_id"].encode()).digest()
    if auth_data["rp_id_hash"] != expected_hash:
        raise ValueError("rpIdHash 不匹配")

    cred_id = auth_data["cred_data"]["credential_id"]
    cose_key = auth_data["cred_data"]["cose_key"]
    pubkey = _cose_to_pubkey(cose_key)

    # 验证签名（full attestation 才有 x5c；self-attestation 用 credential 公钥验）
    alg = att_stmt.get("alg")
    sig = att_stmt.get("sig")
    if fmt == "none":
        pass  # 无需验证
    elif fmt == "packed" and sig and "x5c" in att_stmt:
        # 用证书里的公钥验签 attAuthData
        x5c = att_stmt["x5c"][0]
        cert = serialization.load_der_x509_certificate(x5c)
        cert.public_key().verify(sig, auth_data_raw, ec.ECDSA(hashes.SHA256())) if isinstance(cert.public_key(), ec.EllipticCurvePublicKey) else None
    elif fmt == "packed" and sig:
        # self attestation
        _verify_signature(pubkey, sig, auth_data_raw, alg or -7)
    # fmt=none（多数平台认证器）跳过签名验证，信任 authenticator

    # 存公钥（用公钥字节序列化）
    pub_der = pubkey.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    cred = {
        "id": _b64u(cred_id),
        "pubkey_der_b64u": _b64u(pub_der),
        "alg": alg if alg is not None else -7,
        "sign_count": auth_data["sign_count"],
        "aaguid": auth_data["cred_data"]["aaguid"],
        "rp_id": sess["rp_id"],
        "created_at": int(time.time()),
        "transports": ["internal"],
    }

    # 存入 passkeys.json
    keys = _load_passkeys()
    keys = [k for k in keys if k["id"] != cred["id"]]  # 去重
    keys.append(cred)
    _save_passkeys(keys)
    logger.info(f"Passkey 注册成功: {cred['id'][:12]}... rp={sess['rp_id']}")
    return cred


def begin_authentication(rp_id: str):
    """生成登录（认证）选项。返回 (options_dict, session_key)。"""
    keys = _load_passkeys()
    # 只用当前 rp_id 匹配的凭据（Passkey 绑定域名）
    allow = [{"type": "public-key", "id": k["id"], "transports": k.get("transports", ["internal"])}
             for k in keys if k.get("rp_id") == rp_id]
    challenge = os.urandom(32)
    session_key = _b64u(os.urandom(16))
    _put_challenge(session_key, {"challenge": challenge, "type": "authentication",
                                 "ts": time.time(), "rp_id": rp_id})
    options = {
        "challenge": _b64u(challenge),
        "timeout": CHALLENGE_TTL * 1000,
        "rpId": rp_id,
        "allowCredentials": allow,
        "userVerification": "required",
    }
    return options, session_key


def finish_authentication(session_key: str, client_data_json_b64u: str,
                          authenticator_data_b64u: str, signature_b64u: str,
                          credential_id_b64u: str, user_handle_b64u: str = None):
    """验证登录 assertion。成功返回 True，失败抛异常。"""
    sess = _pop_challenge(session_key)
    if not sess or sess["type"] != "authentication":
        raise ValueError("登录会话无效或已过期")

    client_data = json.loads(_b64u_decode(client_data_json_b64u))
    if client_data.get("type") != "webauthn.get":
        raise ValueError("clientData type 错误")
    if _b64u_decode(client_data.get("challenge", "")) != sess["challenge"]:
        raise ValueError("challenge 不匹配")

    # 找到对应凭据
    keys = _load_passkeys()
    cred = next((k for k in keys if k["id"] == credential_id_b64u), None)
    if not cred:
        raise ValueError("未注册的凭据")

    pubkey = serialization.load_der_public_key(_b64u_decode(cred["pubkey_der_b64u"]))

    auth_data = _b64u_decode(authenticator_data_b64u)
    sig = _b64u_decode(signature_b64u)

    # rpIdHash 校验
    expected_hash = hashlib.sha256(sess["rp_id"].encode()).digest()
    if auth_data[0:32] != expected_hash:
        raise ValueError("rpIdHash 不匹配")

    flags = auth_data[32]
    if not (flags & 0x04):  # UP: user present
        raise ValueError("用户未通过认证器验证")
    # UP+UV 都应置位（userVerification=required）
    if not (flags & 0x08):  # UV: user verified（人脸/指纹通过）
        raise ValueError("未通过生物识别验证")

    # 防重放：signCount 单调递增（平台认证器可能始终为 0，宽松处理）
    new_count = int.from_bytes(auth_data[33:37], "big")
    old_count = cred.get("sign_count", 0)
    if new_count != 0 or old_count != 0:
        if new_count <= old_count:
            logger.warning(f"signCount 未递增: {old_count}->{new_count}（平台认证器可能不计数，放行）")

    # 验签：authenticatorData || SHA256(clientDataJSON)
    client_data_hash = hashlib.sha256(_b64u_decode(client_data_json_b64u)).digest()
    signed = auth_data + client_data_hash
    try:
        _verify_signature(pubkey, sig, signed, cred.get("alg", -7))
    except InvalidSignature:
        raise ValueError("签名验证失败")

    # 更新 signCount
    cred["sign_count"] = max(new_count, old_count)
    keys = [k if k["id"] != cred["id"] else cred for k in keys]
    _save_passkeys(keys)
    logger.info(f"Passkey 登录成功: {cred['id'][:12]}...")
    return True


def list_passkeys():
    return _load_passkeys()


def delete_passkey(cred_id: str):
    keys = _load_passkeys()
    new = [k for k in keys if k["id"] != cred_id]
    _save_passkeys(new)
    return len(keys) - len(new)