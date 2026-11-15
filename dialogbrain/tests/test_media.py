from types import SimpleNamespace
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


def _range_req(value):
	return patch.object(frappe, "request", SimpleNamespace(headers={"Range": value}), create=True)


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

	def test_content_type_is_the_normalised_mime(self):
		frappe.set_user(self.agent_a)
		cases = (
			("image/png\r\nX-Evil: 1", "application/octet-stream", "attachment"),
			("image/png\nSet-Cookie: a=b", "application/octet-stream", "attachment"),
			("IMAGE/PNG; charset=x", "image/png", "inline"),
			(" application/pdf ", "application/pdf", "attachment"),
			("not a mime", "application/octet-stream", "attachment"),
		)
		for i, (mime, ctype, kind) in enumerate(cases):
			fid = str(80 + i)
			msg = _att_msg(self.conv_a, fid, mime=mime, filename="x.bin")
			with patch(CLIENT, _client(file_id=int(fid))), patch(REQUESTS_GET, return_value=_upstream()):
				resp = media.get(msg, fid)
			self.assertEqual(resp.headers["Content-Type"], ctype, repr(mime))
			self.assertTrue(resp.headers["Content-Disposition"].startswith(kind), repr(mime))
			self.assertNotIn("X-Evil", resp.headers)
			self.assertNotIn("Set-Cookie", resp.headers)


	def _av(self, fid, mime="audio/ogg", filename="v.ogg"):
		return _att_msg(self.conv_a, fid, mime=mime, filename=filename)

	def test_range_request_is_forwarded_and_answered_206(self):
		frappe.set_user(self.agent_a)
		msg = self._av("90")
		body = b"x" * 100
		up = _upstream(chunks=(body,), length=100, status=206, ctype="audio/ogg")
		up.headers["Content-Range"] = "bytes 100-199/5000"
		with patch(CLIENT, _client(file_id=90)), patch(REQUESTS_GET, return_value=up) as rg, \
				_range_req("bytes=100-199"):
			resp = media.get(msg, "90")
			got = b"".join(resp.response)
		rg.assert_called_once_with(SECRET_URL, stream=True, timeout=30, headers={"Range": "bytes=100-199"})
		self.assertEqual(resp.status_code, 206)
		self.assertEqual(resp.headers["Content-Range"], "bytes 100-199/5000")
		self.assertEqual(resp.headers["Content-Length"], "100")
		self.assertEqual(got, body)

	def test_open_ended_and_suffix_ranges_are_forwarded(self):
		frappe.set_user(self.agent_a)
		msg = self._av("91")
		for value in ("bytes=100-", "bytes=-500"):
			up = _upstream(chunks=(b"zz",), length=2, status=206, ctype="audio/ogg")
			up.headers["Content-Range"] = "bytes 4998-4999/5000"
			with patch(CLIENT, _client(file_id=91)), patch(REQUESTS_GET, return_value=up) as rg, \
					_range_req(value):
				resp = media.get(msg, "91")
				got = b"".join(resp.response)
			rg.assert_called_once_with(SECRET_URL, stream=True, timeout=30, headers={"Range": value})
			self.assertEqual(resp.status_code, 206, value)
			self.assertEqual(resp.headers["Content-Range"], "bytes 4998-4999/5000")
			self.assertEqual(got, b"zz")

	def test_storage_that_ignores_range_gives_a_full_200(self):
		frappe.set_user(self.agent_a)
		msg = self._av("92")
		up = _upstream(chunks=(b"abc", b"def"), length=6, status=200, ctype="audio/ogg")
		with patch(CLIENT, _client(file_id=92)), patch(REQUESTS_GET, return_value=up), \
				_range_req("bytes=2-3"):
			resp = media.get(msg, "92")
			got = b"".join(resp.response)
		self.assertEqual(resp.status_code, 200)
		self.assertNotIn("Content-Range", resp.headers)
		self.assertEqual(resp.headers["Content-Length"], "6")
		self.assertEqual(got, b"abcdef")

	def test_unsatisfiable_range_is_416_not_500(self):
		frappe.set_user(self.agent_a)
		msg = self._av("93")
		up = _upstream(chunks=(b"<Error/>",), status=416, ctype="application/xml")
		up.headers["Content-Range"] = "bytes */5000"
		with patch(CLIENT, _client(file_id=93)), patch(REQUESTS_GET, return_value=up), \
				_range_req("bytes=9000-"):
			resp = media.get(msg, "93")
			got = b"".join(resp.response)
		self.assertEqual(resp.status_code, 416)
		self.assertEqual(resp.headers["Content-Range"], "bytes */5000")
		self.assertEqual(got, b"")
		self.assertEqual(resp.headers["Cache-Control"], "no-store")
		self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
		self.assertEqual(resp.headers["Content-Security-Policy"], "sandbox; default-src 'none'")
		up.close.assert_called()

	def test_unusable_206_and_416_are_bad_gateway(self):
		frappe.set_user(self.agent_a)
		msg = self._av("96")
		cases = (
			("bytes=0-1", 206, None),
			("bytes=0-1", 206, "garbage"),
			("bytes=0-1", 206, "bytes 0-1/10\nX: y"),
			(None, 206, "bytes 0-1/10"),
			(None, 416, "bytes */10"),
		)
		for rng, status, content_range in cases:
			up = _upstream(chunks=(b"xy",), length=2, status=status, ctype="audio/ogg")
			if content_range:
				up.headers["Content-Range"] = content_range
			with patch(CLIENT, _client(file_id=96)), patch(REQUESTS_GET, return_value=up):
				if rng:
					with _range_req(rng):
						self.assertRaises(media.MediaBadGateway, media.get, msg, "96")
				else:
					self.assertRaises(media.MediaBadGateway, media.get, msg, "96")
			up.close.assert_called()

	def test_malformed_or_multi_range_is_ignored(self):
		frappe.set_user(self.agent_a)
		msg = self._av("94")
		for value in ("bytes=abc", "items=0-1", "bytes=0-1,5-6", "bytes=-", "bytes=1-2\r\nX: y"):
			up = _upstream(chunks=(b"abc", b"def"), length=6, ctype="audio/ogg")
			with patch(CLIENT, _client(file_id=94)), patch(REQUESTS_GET, return_value=up) as rg, \
					_range_req(value):
				resp = media.get(msg, "94")
				got = b"".join(resp.response)
			rg.assert_called_once_with(SECRET_URL, stream=True, timeout=30)
			self.assertEqual(resp.status_code, 200, value)
			self.assertEqual(got, b"abcdef")

	def test_audio_and_video_advertise_accept_ranges(self):
		frappe.set_user(self.agent_a)
		for i, (mime, expect) in enumerate((("audio/ogg", True), ("video/mp4", True),
										   ("image/png", False), ("application/pdf", False))):
			fid = str(100 + i)
			msg = _att_msg(self.conv_a, fid, mime=mime, filename="x.bin")
			with patch(CLIENT, _client(file_id=int(fid))), patch(REQUESTS_GET, return_value=_upstream()):
				resp = media.get(msg, fid)
			if expect:
				self.assertEqual(resp.headers["Accept-Ranges"], "bytes", mime)
			else:
				self.assertNotIn("Accept-Ranges", resp.headers, mime)

	def test_range_keeps_the_security_headers(self):
		frappe.set_user(self.agent_a)
		msg = self._av("95", mime="video/mp4", filename="v.mp4")
		up = _upstream(chunks=(b"xy",), length=2, status=206, ctype="video/mp4")
		up.headers["Content-Range"] = "bytes 0-1/10"
		with patch(CLIENT, _client(file_id=95)), patch(REQUESTS_GET, return_value=up), \
				_range_req("bytes=0-1"):
			resp = media.get(msg, "95")
		self.assertEqual(resp.status_code, 206)
		self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
		self.assertEqual(resp.headers["Content-Security-Policy"], "sandbox; default-src 'none'")
		self.assertTrue(resp.headers["Content-Disposition"].startswith("inline"))
		self.assertEqual(resp.headers["Content-Type"], "video/mp4")
		self.assertEqual(resp.headers["Accept-Ranges"], "bytes")
