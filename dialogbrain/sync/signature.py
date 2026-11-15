import hashlib
import hmac


def verify_signature(raw_body: bytes, header, secret: str) -> bool:
	"""Check ``X-DialogBrain-Signature: sha256=<hex>`` over the raw body.

	The HMAC key is the hex-decoded secret (``whsec_`` prefix stripped).
	"""
	if not header or not secret or not header.startswith("sha256="):
		return False
	try:
		key = bytes.fromhex(secret.removeprefix("whsec_"))
	except ValueError:
		return False
	expected = hmac.new(key, raw_body, hashlib.sha256).hexdigest()
	return hmac.compare_digest(expected, header[len("sha256=") :].strip().lower())
