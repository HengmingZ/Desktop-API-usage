"""三个 AI 编码服务的真实用量数据源。

- Kimi:        OAuth 凭据 ~/.kimi-code/credentials/kimi-code.json
               GET https://api.kimi.com/coding/v1/usages
               刷新: POST https://auth.kimi.com/api/oauth/token (form-encoded)
- ChatGPT:     Codex 凭据 ~/.codex/auth.json
               GET https://chatgpt.com/backend-api/wham/usage
               刷新: POST https://auth.openai.com/oauth/token (form-encoded)
- Antigravity: 本地 Language Server (Connect RPC, 自签名 HTTPS)
               POST https://127.0.0.1:<port>/exa.language_server_pb.LanguageServerService/GetUserStatus

所有凭据仅在本机读取，只发送给各自的第一方服务端点。
"""

from __future__ import annotations

import base64
import json
import re
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_CREATE_NO_WINDOW = 0x08000000
_TIMEOUT = 10


@dataclass
class RingStat:
    """单环数据：短周期（内环，如 5h）或长周期（外环，如周/月）。"""

    label: str  # "5h" / "周" / "月" / "长"
    remaining_percent: int  # 剩余额度 0-100
    reset_at: datetime | None = None


@dataclass
class UsageSnapshot:
    """一次用量查询的结果。inner=5h 窗口，outer=周/月等长周期窗口。"""

    service: str
    inner: RingStat | None = None
    outer: RingStat | None = None
    detail_lines: list[str] = field(default_factory=list)
    ok: bool = True
    error: str | None = None


def _http_json(
    url: str,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    ssl_ctx: ssl.SSLContext | None = None,
) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=_TIMEOUT, context=ssl_ctx) as resp:
        return json.loads(resp.read())


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _jwt_exp(token: str) -> float | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Kimi
# ---------------------------------------------------------------------------


class KimiProvider:
    SERVICE = "Kimi"
    CRED_PATH = Path.home() / ".kimi-code" / "credentials" / "kimi-code.json"
    USAGES_URL = "https://api.kimi.com/coding/v1/usages"
    TOKEN_URL = "https://auth.kimi.com/api/oauth/token"

    def _load_credentials(self) -> dict:
        cred = json.loads(self.CRED_PATH.read_text(encoding="utf-8"))
        # access_token 有效期仅约 15 分钟，剩余不足 2 分钟时刷新
        if time.time() > cred.get("expires_at", 0) - 120:
            cred = self._refresh(cred)
        return cred

    def _refresh(self, cred: dict) -> dict:
        client_id = _jwt_payload_client_id(cred["access_token"])
        body = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": cred["refresh_token"],
                "client_id": client_id,
            }
        ).encode()
        tokens = _http_json(
            self.TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=body,
        )
        cred.update(tokens)
        cred["expires_at"] = time.time() + tokens.get("expires_in", 900)
        self.CRED_PATH.write_text(json.dumps(cred, indent=2), encoding="utf-8")
        return cred

    def fetch(self) -> UsageSnapshot:
        cred = self._load_credentials()
        data = _http_json(
            self.USAGES_URL, headers={"Authorization": f"Bearer {cred['access_token']}"}
        )
        usages = data.get("usages") or {}

        five_h = usages.get("limit_5h") or {}
        used_ratio = five_h.get("used_ratio", 0.0)
        reset_at = _parse_time(five_h.get("reset_time"))

        detail: list[str] = []
        # limits[] 提供 5 小时窗口的绝对次数
        for item in data.get("limits") or []:
            window = item.get("window") or {}
            d = item.get("detail") or {}
            if window.get("timeUnit") == "TIME_UNIT_MINUTE":
                hours = window.get("duration", 0) / 60
                detail.append(
                    f"{hours:g}h window: {d.get('used')}/{d.get('limit')} used"
                )
        month = usages.get("limit_month_total") or {}
        if "used_ratio" in month:
            detail.append(f"Monthly quota: {month['used_ratio'] * 100:.1f}% used")
        wallet = data.get("booster_wallet") or {}
        if wallet.get("status") == "STATUS_ENABLED":
            detail.append("Booster wallet: enabled")
        else:
            detail.append("Booster wallet: disabled")

        inner = RingStat("5h", round((1 - used_ratio) * 100), reset_at)
        # 新版会员无周配额，外环退化为月度总配额
        weekly = next(
            (usages[k] for k in usages if "week" in k or "7d" in k), None
        )
        if weekly:
            outer = RingStat(
                "Week", round((1 - weekly.get("used_ratio", 0.0)) * 100),
                _parse_time(weekly.get("reset_time")),
            )
        elif "used_ratio" in month:
            outer = RingStat(
                "Month", round((1 - month["used_ratio"]) * 100),
                _parse_time(month.get("reset_time")),
            )
        else:
            outer = None

        return UsageSnapshot(
            service=self.SERVICE, inner=inner, outer=outer, detail_lines=detail
        )


def _jwt_payload_client_id(token: str) -> str | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("client_id")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# ChatGPT (Codex)
# ---------------------------------------------------------------------------


class ChatGPTProvider:
    SERVICE = "ChatGPT"
    AUTH_PATH = Path.home() / ".codex" / "auth.json"
    USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
    TOKEN_URL = "https://auth.openai.com/oauth/token"
    CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"  # Codex 公开客户端 ID

    def _load_tokens(self) -> dict:
        auth = json.loads(self.AUTH_PATH.read_text(encoding="utf-8"))
        tokens = auth.get("tokens") or {}
        exp = _jwt_exp(tokens.get("access_token", ""))
        if exp is None or time.time() > exp - 300:
            tokens = self._refresh(auth, tokens)
        return tokens

    def _refresh(self, auth: dict, tokens: dict) -> dict:
        body = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": self.CLIENT_ID,
            }
        ).encode()
        new = _http_json(
            self.TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=body,
        )
        tokens.update({k: v for k, v in new.items() if k in tokens or k.endswith("token")})
        auth["tokens"] = tokens
        self.AUTH_PATH.write_text(json.dumps(auth, indent=2), encoding="utf-8")
        return tokens

    def fetch(self) -> UsageSnapshot:
        tokens = self._load_tokens()
        data = _http_json(
            self.USAGE_URL,
            headers={
                "Authorization": f"Bearer {tokens['access_token']}",
                "chatgpt-account-id": tokens.get("account_id", ""),
                "User-Agent": "codex-cli",
            },
        )
        rl = data.get("rate_limit") or {}
        primary = rl.get("primary_window") or {}
        secondary = rl.get("secondary_window") or {}

        def to_ring(window: dict) -> RingStat | None:
            if not window:
                return None
            secs = window.get("limit_window_seconds", 0)
            label = f"{secs / 3600:g}h" if secs < 86400 else f"{secs / 86400:g}d"
            reset = window.get("reset_at")
            return RingStat(
                label,
                100 - int(window.get("used_percent", 0)),
                datetime.fromtimestamp(reset, tz=timezone.utc) if reset else None,
            )

        detail = [f"Plan: {data.get('plan_type', 'unknown')}"]
        if primary:
            h = primary.get("limit_window_seconds", 0) / 3600
            detail.append(f"{h:g}h window: {primary.get('used_percent', 0)}% used")
        if secondary:
            d = secondary.get("limit_window_seconds", 0) / 86400
            detail.append(f"{d:g}d window: {secondary.get('used_percent', 0)}% used")
        credits = data.get("credits") or {}
        if credits.get("has_credits"):
            detail.append(f"Credit balance: {credits.get('balance')}")

        return UsageSnapshot(
            service=self.SERVICE,
            inner=to_ring(primary),
            outer=to_ring(secondary),
            detail_lines=detail,
        )


# ---------------------------------------------------------------------------
# Antigravity（本地 Language Server）
# ---------------------------------------------------------------------------


class AntigravityProvider:
    SERVICE = "Antigravity"
    RPC_PATH = "/exa.language_server_pb.LanguageServerService/GetUserStatus"

    def __init__(self) -> None:
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    # -- 进程与端口发现 --

    def _find_server(self) -> tuple[str, list[int]]:
        """返回 (csrf_token, 候选端口列表)。找不到进程时抛异常。"""
        ps = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name like '%language_server%'\""
                " | Where-Object { $_.CommandLine -match 'antigravity' }"
                " | Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress",
            ],
            capture_output=True, text=True, timeout=20,
            creationflags=_CREATE_NO_WINDOW,
        )
        pid = None
        csrf = None
        for proc in _as_list(ps.stdout):
            cmd = proc.get("CommandLine") or ""
            m = re.search(r"--csrf_token[=\s]+([^\s\"']+)", cmd)
            if m:
                pid, csrf = proc.get("ProcessId"), m.group(1)
                break
        if not pid or not csrf:
            raise RuntimeError("Antigravity language server not found (is the IDE running?)")

        ports: list[int] = []
        m = re.search(r"--extension_server_port[=\s]+(\d+)", cmd)
        if m:
            ports.append(int(m.group(1)))
        netstat = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=20,
            creationflags=_CREATE_NO_WINDOW,
        )
        for line in netstat.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[-1] == str(pid) and parts[3] == "LISTENING":
                pm = re.search(r":(\d+)$", parts[1])
                if pm:
                    port = int(pm.group(1))
                    if port not in ports:
                        ports.append(port)
        if not ports:
            raise RuntimeError("No listening port found for the Antigravity language server")
        return csrf, ports

    # -- 配额查询 --

    def fetch(self) -> UsageSnapshot:
        csrf, ports = self._find_server()
        body = json.dumps(
            {"metadata": {"ideName": "antigravity", "extensionName": "antigravity",
                          "locale": "en"}}
        ).encode()
        headers = {
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "X-Codeium-Csrf-Token": csrf,
        }
        data = None
        for scheme in ("https", "http"):
            for port in ports:
                try:
                    data = _http_json(
                        f"{scheme}://127.0.0.1:{port}{self.RPC_PATH}",
                        headers=headers, data=body,
                        ssl_ctx=self._ssl_ctx if scheme == "https" else None,
                    )
                    break
                except Exception:
                    continue
            if data is not None:
                break
        if data is None:
            raise RuntimeError("Cannot reach Antigravity local server (port probing failed)")

        status = data.get("userStatus") or data
        configs = (
            (status.get("cascadeModelConfigData") or {}).get("clientModelConfigs") or []
        )
        models = []
        for cfg in configs:
            quota = cfg.get("quotaInfo") or {}
            remaining = quota.get("remainingFraction")
            if remaining is None:
                continue
            models.append(
                (
                    cfg.get("label") or (cfg.get("modelOrAlias") or {}).get("model", "?"),
                    float(remaining),
                    _parse_time(quota.get("resetTime")),
                )
            )
        if not models:
            raise RuntimeError("Antigravity returned no model quota data")

        # 按重置窗口长短分组：≤12 小时视为短周期（内环），其余为长周期（外环）
        now = datetime.now(timezone.utc)
        short: list[tuple[str, float, datetime | None]] = []
        long_: list[tuple[str, float, datetime | None]] = []
        for m in models:
            delta_h = ((m[2] - now).total_seconds() / 3600) if m[2] else 0
            (short if delta_h <= 12 else long_).append(m)

        def worst_of(group: list, label: str) -> RingStat | None:
            if not group:
                return None
            _, remaining, reset = min(group, key=lambda m: m[1])
            return RingStat(label, round(remaining * 100), reset)

        # 明细按配额分组展示最紧的几项
        groups: dict[tuple[float, datetime | None], list[str]] = {}
        for label, remaining, reset in models:
            groups.setdefault((remaining, reset), []).append(label)
        groups_sorted = sorted(groups.items(), key=lambda kv: kv[0][0])

        detail = []
        if status.get("email"):
            detail.append(f"Account: {status['email']}")
        for (remaining, _), labels in groups_sorted[:3]:
            shown = ", ".join(sorted(labels)[:2])
            suffix = " etc." if len(labels) > 2 else ""
            detail.append(f"{shown}{suffix}: {remaining * 100:.0f}% left")

        return UsageSnapshot(
            service=self.SERVICE,
            inner=worst_of(short, "5h"),
            outer=worst_of(long_, "Long"),
            detail_lines=detail,
        )


def _as_list(json_text: str) -> list[dict]:
    text = json_text.strip()
    if not text:
        return []
    data = json.loads(text)
    return data if isinstance(data, list) else [data]


PROVIDERS = {
    "Kimi": KimiProvider,
    "ChatGPT": ChatGPTProvider,
    "Antigravity": AntigravityProvider,
}
