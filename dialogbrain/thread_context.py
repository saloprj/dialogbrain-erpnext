"""Thread context of an agent tool call (Plan 3).

DialogBrain signs the thread an agent run belongs to (``X-DialogBrain-Thread-Id``,
``-Timestamp``, ``-Context-Signature``). Task B2 verifies those headers here; until then
no call carries a context, so every call is treated as unidentified.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Context:
	thread_id: str
	conversation: str
	party_type: str | None
	party: str | None
	identified: bool


def verify(request=None) -> Context | None:
	"""The verified context of the current request, or None (B2 implements verification)."""
	return None
