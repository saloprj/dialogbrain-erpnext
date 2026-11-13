"""HTTP client for the DialogBrain api-gateway.

Auth: ``Authorization: Bearer db_live_...`` (one key = one workspace).
The API key is never included in exceptions or logs; use ``mask()`` to log it.
"""

import requests


def mask(secret: str) -> str:
	"""``db_live_abcdef1234`` -> ``db_live_…1234`` (safe to log)."""
	return "db_live_…" + (secret or "")[-4:]


class DialogBrainError(Exception):
	def __init__(self, message, status=None, code=None):
		super().__init__(message)
		self.status = status
		self.code = code


class DialogBrainClient:
	def __init__(self, base_url: str, api_key: str, timeout: float = 20):
		self.base_url = (base_url or "").rstrip("/")
		self._api_key = api_key
		self.timeout = timeout
		self._session = requests.Session()

	@classmethod
	def from_settings(cls):
		import frappe

		settings = frappe.get_cached_doc("DialogBrain Settings")
		api_key = settings.get_password("api_key", raise_exception=False)
		if not settings.base_url or not api_key:
			raise DialogBrainError("DialogBrain Settings: base URL and API key are required")
		return cls(settings.base_url, api_key)

	# -- internals -----------------------------------------------------

	def _scrub(self, text) -> str:
		text = str(text)
		if self._api_key:
			text = text.replace(self._api_key, mask(self._api_key))
		return text

	def _request(self, method, path, json=None, params=None):
		headers = {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}
		kwargs = {"headers": headers, "timeout": self.timeout}
		if json is not None:
			kwargs["json"] = json
		if params is not None:
			kwargs["params"] = params
		try:
			resp = self._session.request(method, self.base_url + path, **kwargs)
		except requests.RequestException as e:
			raise DialogBrainError(f"DialogBrain request failed: {self._scrub(e)}") from None
		if resp.status_code >= 400:
			detail = None
			code = None
			try:
				body = resp.json()
				detail = body.get("detail") if isinstance(body, dict) else None
			except ValueError:
				pass
			if detail is None:
				detail = (resp.text or "")[:300]
			if isinstance(detail, dict):  # e.g. {"code": "INVALID_API_KEY", "message": "..."}
				code = detail.get("code")
				detail = detail.get("message") or str(detail)
			if not isinstance(detail, str):
				detail = str(detail)[:300]
			raise DialogBrainError(
				f"DialogBrain HTTP {resp.status_code}: {self._scrub(detail)}",
				status=resp.status_code,
				code=code,
			)
		return resp

	@staticmethod
	def _json(resp):
		try:
			return resp.json()
		except ValueError:
			raise DialogBrainError("DialogBrain returned a non-JSON response", status=resp.status_code) from None

	# -- tools ---------------------------------------------------------

	def execute(self, tool_id: str, params: dict, thread_id: str | None = None):
		body = {"tool_id": tool_id, "params": params, "mode": "auto", "skip_approval": True}
		if thread_id is not None:
			body["thread_id"] = thread_id
		payload = self._json(self._request("POST", f"/api/tools/{tool_id}/execute", json=body))
		if not isinstance(payload, dict):
			raise DialogBrainError("Unexpected DialogBrain response shape")
		if payload.get("requires_approval"):
			raise DialogBrainError(
				self._scrub(payload.get("message") or "approval required"), code="requires_approval"
			)
		if not payload.get("success"):
			err = payload.get("error")
			code = err.get("code") if isinstance(err, dict) else err
			msg = err.get("message") if isinstance(err, dict) else err
			raise DialogBrainError(self._scrub(msg or f"{tool_id} failed"), code=code)
		return payload.get("data")

	def list_tools(self) -> set:
		"""Tool ids available to this key (GET /api/tools/catalog)."""
		payload = self._json(self._request("GET", "/api/tools/catalog"))
		return {t["id"] for t in payload.get("tools", [])}

	# -- webhooks ------------------------------------------------------

	def create_webhook(self, url: str, event_types: list) -> dict:
		body = {"url": url, "event_types": list(event_types)}
		return self._json(self._request("POST", "/api/v1/webhooks", json=body))

	def delete_webhook(self, webhook_id) -> None:
		self._request("DELETE", f"/api/v1/webhooks/{webhook_id}")

	def activate_webhook(self, webhook_id) -> None:
		self._request("PATCH", f"/api/v1/webhooks/{webhook_id}/activate")

	def list_webhooks(self) -> list:
		return self._json(self._request("GET", "/api/v1/webhooks"))

	def webhook_deliveries(self, webhook_id, limit: int = 10) -> list:
		return self._json(
			self._request("GET", f"/api/v1/webhooks/{webhook_id}/deliveries", params={"limit": limit})
		)
