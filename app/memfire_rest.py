# app/memfire_rest.py
import os

import requests


class MemFireError(RuntimeError):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


class MemFireREST:
    """MemFire/PostgREST 简单客户端。仅供 Flask 后端使用。"""

    def __init__(self):
        api_url = os.environ.get("MEMFIRE_API_URL", "").strip().rstrip("/")
        api_key = os.environ.get("MEMFIRE_SERVICE_ROLE_KEY", "").strip()

        if not api_url:
            raise RuntimeError("缺少环境变量 MEMFIRE_API_URL")
        if not api_key:
            raise RuntimeError("缺少环境变量 MEMFIRE_SERVICE_ROLE_KEY")

        if not api_url.endswith("/rest/v1"):
            api_url += "/rest/v1"

        self.base_url = api_url
        self.api_key = api_key
        self.timeout = float(os.environ.get("MEMFIRE_TIMEOUT", "20"))

    def request(self, method, table, *, params=None, body=None, prefer=None):
        if not table.replace("_", "").isalnum():
            raise ValueError("不合法的表名")

        headers = {
            "apikey": self.api_key,
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Accept-Profile": "public",
            "Content-Profile": "public",
        }

        if body is not None:
            headers["Content-Type"] = "application/json"

        if prefer:
            headers["Prefer"] = prefer

        url = f"{self.base_url}/{table}"

        try:
            response = requests.request(
                method=method,
                url=url,
                params=params,
                json=body,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise MemFireError(0, f"请求 MemFire 失败：{exc}") from exc

        if not response.ok:
            detail = response.text[:1500]
            raise MemFireError(
                response.status_code,
                f"MemFire HTTP {response.status_code}: {detail}",
            )

        if not response.content:
            return []

        try:
            data = response.json()
        except ValueError:
            return []

        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
        return []

    def select(self, table, *, filters=None, select="*", order=None, limit=None):
        params = {"select": select}
        params.update(filters or {})
        if order:
            params["order"] = order
        if limit is not None:
            params["limit"] = str(limit)
        return self.request("GET", table, params=params)

    def insert(self, table, rows):
        result = self.request(
            "POST",
            table,
            body=rows,
            prefer="return=representation",
        )
        return result

    def update(self, table, filters, values):
        return self.request(
            "PATCH",
            table,
            params=filters,
            body=values,
            prefer="return=representation",
        )

    def delete(self, table, filters):
        return self.request(
            "DELETE",
            table,
            params=filters,
            prefer="return=representation",
        )


def get_memfire():
    return MemFireREST()