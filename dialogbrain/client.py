"""HTTP client for the DialogBrain api-gateway.

Auth: ``Authorization: Bearer db_live_...`` (one key = one workspace).
The API key is never included in exceptions or logs; use ``mask()`` to log it.
"""

import hashlib

import requests


def mask(secret: str) -> str:
	"""``db_live_abcdef1234`` -> ``db_…1234`` (safe to log; prefix-agnostic)."""
	return "db_…" + (secret or "")[-4:]


class DialogBrainError(Exception):
	def __init__(self, message, status=None, code=None):
		super().__init__(message)
		self.status = status
		self.code = code


class DialogBrainTimeout(DialogBrainError):
	"""The request reached (or may have reached) DialogBrain but no answer came back.

	The call may or may not have taken effect, so a non-idempotent call (``messages.send``)
	must not be retried blindly. A connect timeout is NOT this (nothing was sent).
	"""


class DialogBrainClient:
	def __init__(self, base_url: str, api_key: str, timeout: float = 20):
		self.base_url = (base_url or "").rstrip("/")
		self._api_key = api_key
		self.timeout = timeout
		self._session = requests.Session()
		# ``request_id`` echoed by the last ``execute`` (DialogBrain with Part A1), else None
		self.last_request_id = None

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
		except requests.ConnectTimeout as e:
			raise DialogBrainError(f"DialogBrain request failed: {self._scrub(e)}") from None
		except requests.Timeout as e:
			raise DialogBrainTimeout(f"DialogBrain request timed out: {self._scrub(e)}") from None
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

	def execute(self, tool_id: str, params: dict, thread_id: str | None = None, request_id: str | None = None):
		"""Run a tool; returns its ``data``.

		``request_id`` (optional) is DialogBrain's idempotency key: a repeat of the same call
		with the same id returns the first result instead of running again. DialogBrain
		with that support echoes it at the top level of the response; the echo (or None)
		is kept in ``self.last_request_id``. An older DialogBrain ignores the field."""
		body = {"tool_id": tool_id, "params": params, "mode": "auto", "skip_approval": True}
		if thread_id is not None:
			body["thread_id"] = thread_id
		if request_id is not None:
			body["request_id"] = request_id
		self.last_request_id = None
		payload = self._json(self._request("POST", f"/api/tools/{tool_id}/execute", json=body))
		if not isinstance(payload, dict):
			raise DialogBrainError("Unexpected DialogBrain response shape")
		echoed = payload.get("request_id")
		self.last_request_id = echoed if isinstance(echoed, str) else None
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

	def upload_file(self, filename: str, content: bytes, mime_type: "str | None" = None) -> int:
		"""Upload bytes as a workspace file; returns its ``file_id`` (usable in messages.send).

		``files.create_upload_url`` -> HTTP PUT to the presigned URL (headers verbatim, no
		API key) -> ``files.complete_upload``. A sha256 the workspace already holds comes back
		``deduplicated`` and needs no upload.
		"""
		params = {
			"filename": filename,
			"mime_type": mime_type,
			"size_bytes": len(content),
			"sha256": hashlib.sha256(content).hexdigest(),
		}
		ticket = self.execute("files.create_upload_url", {k: v for k, v in params.items() if v is not None})
		if not isinstance(ticket, dict) or ticket.get("file_id") is None:
			raise DialogBrainError("files.create_upload_url returned no file_id")
		file_id = ticket["file_id"]
		if ticket.get("deduplicated"):
			return file_id
		if not ticket.get("upload_url"):
			raise DialogBrainError("files.create_upload_url returned no upload_url")
		try:
			resp = requests.request(
				ticket.get("method") or "PUT",
				ticket["upload_url"],
				data=content,
				headers=dict(ticket.get("headers") or {}),
				timeout=max(self.timeout, 60),
			)
		except requests.RequestException as e:
			raise DialogBrainError(f"File upload failed: {type(e).__name__}") from None
		if resp.status_code >= 400:
			raise DialogBrainError(f"File upload failed: HTTP {resp.status_code}", status=resp.status_code)
		self.execute("files.complete_upload", {"file_id": file_id})
		return file_id

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
