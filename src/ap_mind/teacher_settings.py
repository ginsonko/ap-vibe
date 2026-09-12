"""Optional bounded teacher; Windows credentials are protected by user DPAPI."""
from contextlib import closing
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import CapabilityOwnership, ContractError, utc_now
from .gateway import NullGateway, OpenAICompatibleGateway, UrllibModelTransport, GatewayTransportError
from .governance import GovernanceCompatibilityRecord
from .knowledge_correction import NullKnowledgeCorrectionInterpreter


def protect(raw: bytes, decrypt=False) -> bytes:
    if os.name != "nt":
        from .posix_secrets import protect as posix_protect
        return posix_protect(raw, decrypt=decrypt)
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    dll = ctypes.WinDLL("crypt32", use_last_error=True)
    method = dll.CryptUnprotectData if decrypt else dll.CryptProtectData
    method.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    method.restype = wintypes.BOOL
    if not method(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ContractError("teacher_secure_storage_failed")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        free = ctypes.WinDLL("kernel32").LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(target.data)


class BudgetTransport(UrllibModelTransport):
    def __init__(self, settings):
        self.settings = settings

    def request_json(self, *args, **kwargs):
        owner = self.settings
        with closing(owner.service.product_registry._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            hour = utc_now()[:13]
            count = connection.execute("SELECT COUNT(*) FROM teacher_call_budget WHERE hour=?", (hour,)).fetchone()[0]
            limit = owner.value.get("calls_per_hour")
            if not owner.value.get("enabled") or (limit is not None and count >= limit):
                raise GatewayTransportError("teacher_hourly_limit_or_disabled")
            connection.execute("INSERT INTO teacher_call_budget(hour,created_at) VALUES (?,?)", (hour, utc_now()))
            connection.commit()
        return super().request_json(*args, **kwargs)


class TeacherSettings:
    def __init__(self, service):
        self.service = service
        self.path = service.data_dir / "private" / "teacher.dpapi"
        # A budget is an optional user protection. None deliberately means
        # observe the count without imposing a product-level hourly ceiling.
        self.value = {"enabled": False, "base_url": "", "model": "", "api_key": "", "calls_per_hour": None}
        self.error = None
        with closing(service.product_registry._connect()) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS teacher_call_budget(id INTEGER PRIMARY KEY, hour TEXT NOT NULL, created_at TEXT NOT NULL)")
            connection.commit()
        if self.path.exists():
            try:
                loaded = json.loads(protect(self.path.read_bytes(), decrypt=True))
                if not isinstance(loaded, dict):
                    raise ValueError("teacher settings must be an object")
                self.value = {**self.value, **loaded}
            except Exception:
                self.error = "凭据无法解密，已停用外部教师。请在当前 Windows 用户下重新配置。"

    def state(self):
        with closing(self.service.product_registry._connect()) as connection:
            count = connection.execute("SELECT COUNT(*) FROM teacher_call_budget WHERE hour=?", (utc_now()[:13],)).fetchone()[0]
        return {k: self.value[k] for k in ("enabled", "base_url", "model", "calls_per_hour")} | {
            "key_saved": bool(self.value.get("api_key")), "storage": "Windows 当前用户加密",
            "attempts_this_hour": count, "error": self.error, "paid_probe_performed": False,
            "multimodal_input": "not_connected", "cost": None}

    def update(self, raw):
        if not isinstance(raw.get("enabled"), bool):
            raise ContractError("teacher_enabled_boolean_required")
        value = {**self.value, **{k: raw[k] for k in ("enabled", "base_url", "model", "calls_per_hour") if k in raw}}
        if raw.get("api_key"):
            value["api_key"] = raw["api_key"]
        if raw.get("forget_key"):
            value["api_key"] = ""
            value["enabled"] = False
        for key in ("base_url", "model", "api_key"):
            if not isinstance(value[key], str) or len(value[key]) > 2048 or any(c in value[key] for c in "\r\n"):
                raise ContractError("teacher_configuration_invalid")
            value[key] = value[key].strip()
        if value["base_url"]:
            url = urlsplit(value["base_url"])
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ContractError("teacher_url_invalid")
            if url.scheme == "http" and url.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ContractError("teacher_remote_https_required")
        if value["enabled"] and not all(value[k] for k in ("base_url", "model", "api_key")):
            raise ContractError("teacher_configuration_incomplete")
        limit = value["calls_per_hour"]
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
            raise ContractError("teacher_budget_invalid")
        with self.service._lock:
            encrypted = protect(json.dumps(value).encode())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_bytes(encrypted)
            os.replace(temp, self.path)
            self.value = value
            self.error = None
            self.apply()
        return {"ok": True, **self.state(), "message": "配置已保存；未发送测试请求。开启后新活动才可能使用教师。"}

    def apply(self):
        from .studio_server import HYBRID_WHITEPAPER_SHA256
        service = self.service
        service.correction_interpreter = NullKnowledgeCorrectionInterpreter()
        if not self.value["enabled"]:
            service.gateway, service.governance, service.capability = NullGateway(), None, None
            return
        service.gateway = OpenAICompatibleGateway(self.value["base_url"], self.value["api_key"], self.value["model"],
            advisor_role="joint_cognition_teacher", timeout=20, max_retries=0, max_prompt_chars=24000,
            max_output_tokens=1200, transport=BudgetTransport(self))
        service.governance = GovernanceCompatibilityRecord(record_id="user-optional-teacher", project_id=service.codex_project_id,
            whitepaper_sha256=HYBRID_WHITEPAPER_SHA256, compatibility="compatible", llm_delegation_enabled=True,
            authority_sources={"user_authorization": "local-teacher-settings", "whitepaper": HYBRID_WHITEPAPER_SHA256},
            next_action="retain AP decision ownership and evidence boundaries")
        service.capability = CapabilityOwnership(capability_key="hybrid.cognition", stage="assisted", decision_owner="ap_native",
            content_owner="mixed", evidence_owner="environment", execution_owner="environment",
            scope=("environment:ap-vibe", "low_risk_existing_action_candidates"), llm_allowed=True,
            reason="User enabled optional bounded cognition teacher", version="0.1.0")
