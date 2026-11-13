from unittest.mock import MagicMock, patch

import frappe

from dialogbrain.api import media
from dialogbrain.client import DialogBrainError, DialogBrainTimeout
from dialogbrain.tests.test_inbox_api import _channel, _conv, _msg, _user
from dialogbrain.tests.utils import DialogBrainTestCase

CLIENT = "dialogbrain.api.media.DialogBrainClient"
REQUESTS_GET = "dialogbrain.api.media.requests.get"
SECRET_URL = "https://files.example.test/signed?sig=SECRETSIG"


def _att_msg(conversation, file_id="42", mime="image/png", filename="pic.png"):
	return _msg(
		conversation,
		attachments=[{"file_id": file_id, "filename": filename, "mime_type": mime,
					  "media_type": "image", "size": 10}],
	)


def _upstream(chunks=(b"abc", b"def"), length=None, status=200, ctype="image/png"):
	up = MagicMock()
	up.status_code = status
	up.headers = {"Content-Type": ctype}
	if length is not None:
		up.headers["Content-Length"] = str(length)
	up.iter_content.return_value = iter(chunks)
	return up


def _client(url=SECRET_URL, file_id=42):
	c = MagicMock()
	c.from_settings.return_value.execute.return_value = {
		"files": [{"file_id": file_id, "download_url": url}], "not_found": []}
	return c


class TestMedia(DialogBrainTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		for dt in ("DB Message", "DB Conversation", "DB Channel User", "DB Channel"):
			frappe.db.delete(dt)
		self.agent_a = _user("media-agent-a@example.com", "Chat Agent")
		self.agent_b = _user("media-agent-b@example.com", "Chat Agent")
		_channel("media-a", "telegram", [self.agent_a])
		_channel("media-b", "whatsapp", [self.agent_b])
		self.conv_a = _conv("media-a")
		self.conv_b = _conv("media-b")
		self.msg_a = _att_msg(self.conv_a, "42")
		self.msg_a2 = _att_msg(self.conv_a, "43")
		self.msg_b = _att_msg(self.conv_b, "99")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_happy_path_streams_with_headers(self):
		frappe.set_user(self.agent_a)
		up = _upstream(length=6)
		client = _client()
		with patch(CLIENT, client), patch(REQUESTS_GET, return_value=up) as rg:
			resp = media.get(self.msg_a, "42")
			body = b"".join(resp.response)
		client.from_settings.return_value.execute.assert_called_once_with("files.info", {"file_ids": [42]})
		rg.assert_called_once_with(SECRET_URL, stream=True, timeout=30)
		self.assertEqual(body, b"abcdef")
		self.assertTrue(resp.direct_passthrough)
		self.assertEqual(resp.headers["Content-Type"], "image/png")
		self.assertEqual(resp.headers["Cache-Control"], "private, max-age=300")
		self.assertTrue(resp.headers["Content-Disposition"].startswith("inline"))
		up.close.assert_called()

	def test_non_media_is_attachment(self):
		msg = _att_msg(self.conv_a, "50", mime="application/pdf", filename="a.pdf")
		frappe.set_user(self.agent_a)
		with patch(CLIENT, _client(file_id=50)), patch(REQUESTS_GET, return_value=_upstream()):
			resp = media.get(msg, "50")
		self.assertTrue(resp.headers["Content-Disposition"].startswith("attachment"))

	def test_file_id_of_other_message_refused(self):
		frappe.set_user(self.agent_a)
		client = _client()
		with patch(CLIENT, client), patch(REQUESTS_GET) as rg:
			# another message of the same (visible) conversation
			self.assertRaises(frappe.PermissionError, media.get, self.msg_a, "43")
			# a file_id of a different conversation's message
			self.assertRaises(frappe.PermissionError, media.get, self.msg_a, "99")
		client.from_settings.assert_not_called()
		rg.assert_not_called()

	def test_no_access_refused(self):
		frappe.set_user(self.agent_a)
		with patch(CLIENT, _client()) as c, patch(REQUESTS_GET) as rg:
			self.assertRaises(frappe.PermissionError, media.get, self.msg_b, "99")
		c.from_settings.assert_not_called()
		rg.assert_not_called()

	def test_unknown_message_refused(self):
		frappe.set_user(self.agent_a)
		self.assertRaises(frappe.PermissionError, media.get, "nope-nope", "42")

	def test_oversize_content_length_refused(self):
		frappe.set_user(self.agent_a)
		up = _upstream(length=media.MEDIA_MAX_BYTES + 1)
		with patch(CLIENT, _client()), patch(REQUESTS_GET, return_value=up):
			with self.assertRaises(media.MediaTooLarge) as cm:
				media.get(self.msg_a, "42")
		self.assertEqual(cm.exception.http_status_code, 413)
		up.close.assert_called()

	def test_oversize_while_streaming_stops(self):
		frappe.set_user(self.agent_a)
		with patch.object(media, "MEDIA_MAX_BYTES", 5):
			up = _upstream(chunks=(b"abc", b"def", b"ghi"))
			with patch(CLIENT, _client()), patch(REQUESTS_GET, return_value=up):
				resp = media.get(self.msg_a, "42")
				got = []
				with self.assertRaises(media.MediaTooLarge):
					for chunk in resp.response:
						got.append(chunk)
		self.assertEqual(got, [b"abc"])
		up.close.assert_called()

	def test_dialogbrain_error_is_502_without_url(self):
		frappe.set_user(self.agent_a)
		for exc in (DialogBrainError(f"boom {SECRET_URL}"), DialogBrainTimeout("slow")):
			c = MagicMock()
			c.from_settings.return_value.execute.side_effect = exc
			with patch(CLIENT, c), patch(REQUESTS_GET):
				with self.assertRaises(media.MediaBadGateway) as cm:
					media.get(self.msg_a, "42")
			self.assertEqual(cm.exception.http_status_code, 502)
			self.assertNotIn("SECRETSIG", str(cm.exception))

	def test_download_failure_is_502_without_url(self):
		import requests

		frappe.set_user(self.agent_a)
		with patch(CLIENT, _client()), patch(
			REQUESTS_GET, side_effect=requests.ConnectionError(f"x {SECRET_URL}")):
			with self.assertRaises(media.MediaBadGateway) as cm:
				media.get(self.msg_a, "42")
		self.assertNotIn("SECRETSIG", str(cm.exception))
		up = _upstream(status=403)
		with patch(CLIENT, _client()), patch(REQUESTS_GET, return_value=up):
			self.assertRaises(media.MediaBadGateway, media.get, self.msg_a, "42")
		up.close.assert_called()

	def test_missing_download_url_is_502(self):
		frappe.set_user(self.agent_a)
		c = MagicMock()
		c.from_settings.return_value.execute.return_value = {"files": [], "not_found": [42]}
		with patch(CLIENT, c):
			self.assertRaises(media.MediaBadGateway, media.get, self.msg_a, "42")

	def test_dangerous_types_are_attachments_with_hardening_headers(self):
		frappe.set_user(self.agent_a)
		for i, mime in enumerate(("image/svg+xml", "text/html", "image/ png", "application/xml",
								  "image/png; charset=x", "IMAGE/PNG")):
			fid = str(60 + i)
			msg = _att_msg(self.conv_a, fid, mime=mime, filename="x.bin")
			with patch(CLIENT, _client(file_id=int(fid))), patch(REQUESTS_GET, return_value=_upstream()):
				resp = media.get(msg, fid)
			expect = "inline" if mime in ("image/png; charset=x", "IMAGE/PNG") else "attachment"
			self.assertTrue(resp.headers["Content-Disposition"].startswith(expect), mime)
			self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
			self.assertEqual(resp.headers["Content-Security-Policy"], "sandbox; default-src 'none'")

	def test_unicode_filename(self):
		msg = _att_msg(self.conv_a, "70", mime="application/pdf", filename="Договор.pdf")
		frappe.set_user(self.agent_a)
		with patch(CLIENT, _client(file_id=70)), patch(REQUESTS_GET, return_value=_upstream()):
			resp = media.get(msg, "70")
		self.assertIn("filename*=UTF-8''%D0%94%D0%BE%D0%B3%D0%BE%D0%B2%D0%BE%D1%80.pdf",
					  resp.headers["Content-Disposition"])

	def test_bad_files_info_shape_is_502(self):
		frappe.set_user(self.agent_a)
		for data in (None, [], "x", {"files": "x"}, {"files": [None]}):
			c = MagicMock()
			c.from_settings.return_value.execute.return_value = data
			with patch(CLIENT, c):
				self.assertRaises(media.MediaBadGateway, media.get, self.msg_a, "42")

	def test_502_has_no_context_and_no_error_log(self):
		import requests

		frappe.set_user(self.agent_a)
		before = frappe.db.count("Error Log")
		with patch(CLIENT, _client()), patch(
			REQUESTS_GET, side_effect=requests.ConnectionError(f"x {SECRET_URL}")):
			with self.assertRaises(media.MediaBadGateway) as cm:
				media.get(self.msg_a, "42")
		exc = cm.exception
		self.assertTrue(exc.__context__ is None or exc.__suppress_context__)
		self.assertNotIn("SECRETSIG", repr(exc.__context__))
		self.assertTrue(media.MediaBadGateway.skip_error_log)
		from frappe.utils.error import log_error_snapshot
		log_error_snapshot(exc)
		self.assertEqual(frappe.db.count("Error Log"), before)
