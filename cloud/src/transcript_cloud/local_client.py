"""Server-side authenticated cloud requests. Never send credentials to the browser."""

import os
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
        try:
            with httpx.Client(timeout=60, follow_redirects=False) as client:
                response = client.request(
                    method,
                    self.url + "/api" + path,
                    json=body,
                    headers={"Authorization": "Bearer " + self.token},
                )
            if not response.is_success:
                # Do not expose arbitrary provider errors, headers or signed requests.
                if response.status_code == 401:
                    raise CloudError("云端认证失败，请检查 TC_PANEL_TOKEN。")
                if response.status_code == 404 and path.startswith("/studio/"):
                    raise CloudError("云端尚未提供工作台 API，请先部署本版本的云端应用。")
                detail = response.json().get("detail", "")
                if isinstance(detail, str) and len(detail) < 500:
                    raise CloudError(detail)
                raise CloudError(f"云端请求失败（HTTP {response.status_code}）。")
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CloudError("无法连接云端 API，请检查地址、网络和部署版本。") from exc

    def all(self, kind):
        rows, cursor = [], None
        while True:
            page = self.request("GET", f"/{kind}" + (f"?cursor={cursor}" if cursor else ""))
            rows.extend(page["items"])
            cursor = page.get("cursor")
            if not cursor:
                return rows
