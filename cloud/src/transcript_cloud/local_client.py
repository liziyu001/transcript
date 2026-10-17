"""Server-side authenticated cloud requests. Never send credentials to the browser."""

import os
import re
from urllib.parse import urlparse

import httpx


class CloudError(RuntimeError):
    pass


class CloudClient:
    def __init__(self, url=None, token=None):
        self.url = (url if url is not None else os.getenv("TC_API_URL", "")).rstrip("/")
        self.token = token if token is not None else os.getenv("TC_PANEL_TOKEN", "")
        parsed = urlparse(self.url)
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not parsed.hostname
            or not (
                parsed.scheme == "https"
                or (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"})
            )
        ):
            raise ValueError("请在本地环境配置 TC_API_URL（HTTPS 云端 API 地址）。")
        if len(self.token) < 24:
            raise ValueError("请在本地环境配置 TC_PANEL_TOKEN（至少 24 位）。")

    def request(self, method, path, body=None):
        context = f"{method} /api{path.split('?')[0]}"
        try:
            with httpx.Client(timeout=60, follow_redirects=False) as client:
                response = client.request(
                    method,
                    self.url + "/api" + path,
                    json=body,
                    headers={"Authorization": "Bearer " + self.token},
                )
            request_id = response.headers.get("x-fc-request-id", "")
            if re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", request_id):
                context += f" · Request ID: {request_id}"
            if not response.is_success:
                context += f" · HTTP {response.status_code}"
                # Do not expose arbitrary provider errors, headers or signed requests.
                if response.status_code == 401:
                    raise CloudError(f"{context}：云端认证失败，请检查 TC_PANEL_TOKEN。")
                if response.status_code == 403:
                    raise CloudError(
                        f"{context}：云端拒绝请求，请检查 FC HTTP 入口允许的方法及访问权限。"
                    )
                if response.status_code == 404 and path.startswith("/studio/"):
                    raise CloudError(
                        f"{context}：云端尚未提供工作台 API，请先部署本版本的云端应用。"
                    )
                try:
                    data = response.json()
                except ValueError:
                    data = {}
                detail = data.get("detail", "") if isinstance(data, dict) else ""
                if (
                    response.status_code in {400, 409}
                    and isinstance(detail, str)
                    and len(detail) < 500
                ):
                    detail = detail.replace(self.token, "[REDACTED]")
                    detail = re.sub(r"https?://\S+", "[URL REDACTED]", detail)
                    raise CloudError(f"{context}：{detail}")
                raise CloudError(f"{context}：云端请求失败，请用 Request ID 查询 FC 日志。")
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CloudError(
                f"{context}：云端通信失败（{type(exc).__name__}），请检查网络或响应格式。"
            ) from exc

    def all(self, kind):
        rows, cursor = [], None
        while True:
            page = self.request("GET", f"/{kind}" + (f"?cursor={cursor}" if cursor else ""))
            rows.extend(page["items"])
            cursor = page.get("cursor")
            if not cursor:
                return rows
