"""A fake DialogBrain for looking at the "Connect channel" dialog on a dev site.

Speaks just enough of POST /api/tools/<tool>/execute for channels.connect_session.*:
a QR session connects 12 s after start (the QR changes at 6 s); a phone session asks for
a code ("11111"), then a 2FA password ("pw"), then connects; WhatsApp pairing shows a code
after the phone is sent and connects 10 s later. Dev only: never point a real site at it.

    python dev/fake_connect_server.py 9911
"""

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

SESSIONS = {}


def _exp(seconds=180):
	return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _view(sid):
	s = SESSIONS.get(sid)
	if not s:
		return {"status": "expired"}
	age = time.time() - s["t"]
	out = {"status": s["status"], "expires_at": s["exp"]}
	if s["method"] == "qr":
		if age > 12:
			s["status"] = "connected"
		elif age > 9:
			s["status"] = "scanned"
		out["status"] = s["status"]
		out["qr"] = f"fake-qr-{sid}-{int(age // 6)}"
	if s.get("pair_at"):
		out["pairing_code"] = "ABCD-1234"
		if time.time() - s["pair_at"] > 10:
			s["status"] = out["status"] = "connected"
	if s["status"] == "connected":
		out.update({"account_id": s["account"], "phone": "+10000000000", "name": f"Demo {s['channel']} {s['account']}"})
	return out


def run(tool, p):
	if tool.endswith(".start"):
		sid = f"s{len(SESSIONS) + 1}"
		method = p.get("method") or "qr"
		status = {"qr": "waiting_scan", "pairing_code": "need_pairing_code", "phone": "need_phone"}[method]
		SESSIONS[sid] = {"t": time.time(), "status": status, "method": method, "channel": p["channel"],
			"exp": _exp(), "account": f"demo-{int(time.time())}"}
		return {"session_id": sid, **_view(sid)}
	sid = p.get("session_id")
	s = SESSIONS.get(sid)
	if tool.endswith(".submit") and s:
		s["exp"] = _exp()
		if "phone" in p:
			if s["method"] == "pairing_code":
				s["pair_at"] = time.time()
			else:
				s["status"] = "need_code"
		elif "code" in p:
			if p["code"] != "11111":
				return {"__error__": "The code is wrong"}
			s["status"] = "need_password"
		elif "password" in p:
			if p["password"] != "pw":
				return {"__error__": "The password is wrong"}
			s["status"] = "connected"
	if tool.endswith(".cancel"):
		SESSIONS.pop(sid, None)
		return {"ok": True}
	return _view(sid)


class H(BaseHTTPRequestHandler):
	def log_message(self, *a):
		pass

	def _send(self, body):
		raw = json.dumps(body).encode()
		self.send_response(200)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(raw)))
		self.end_headers()
		self.wfile.write(raw)

	def do_POST(self):
		body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
		tool = self.path.split("/")[3]
		data = run(tool, body.get("params") or {})
		if "__error__" in data:
			return self._send({"success": False, "error": data["__error__"]})
		self._send({"success": True, "data": data})


if __name__ == "__main__":
	HTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 9911), H).serve_forever()
