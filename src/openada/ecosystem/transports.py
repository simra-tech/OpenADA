"""Typed agent-session and remote-job transports with executable fake backends."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import secrets
from threading import RLock
from typing import Any, Callable, Mapping
import uuid

from .canonical import canonical_json_bytes
from .contracts import SchemaCatalog


class TransportError(ValueError):
    """A transport request violates ownership, replay, state, or identity policy."""


SessionHandler = Callable[[Mapping[str, Any]], Mapping[str, Any]]
JobHandler = Callable[[Mapping[str, Any]], Mapping[str, Any]]


def _sha256(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True, slots=True)
class SessionHandle:
    session_id: str
    ownership_token: str
    receipt: dict[str, Any]


@dataclass(slots=True)
class _Session:
    session_id: str
    owner_id: str
    nonce_sha256: str
    ownership_token_sha256: str
    backend_revision: str
    handler: SessionHandler
    state: str = "ready"
    heartbeat_sequence: int = 0
    last_sequence: int = 0
    cleanup: str = "not-started"
    replies: dict[str, tuple[str, int, dict[str, Any]]] = field(default_factory=dict)


class AgentSessionTransport:
    """Host-owned session lifecycle with tokens, sequence checks, and replay safety."""

    def __init__(self, schemas: SchemaCatalog | None = None) -> None:
        self._schemas = schemas or SchemaCatalog()
        self._lock = RLock()
        self._sessions: dict[str, _Session] = {}

    @staticmethod
    def _receipt(session: _Session) -> dict[str, Any]:
        return {
            "schema": "openada.session-receipt/v0alpha1",
            "session_id": session.session_id,
            "owner_id": session.owner_id,
            "nonce_sha256": session.nonce_sha256,
            "ownership_token_sha256": session.ownership_token_sha256,
            "state": session.state,
            "backend_revision": session.backend_revision,
            "last_heartbeat_sequence": session.heartbeat_sequence,
            "cleanup": session.cleanup,
            "extensions": {},
        }

    def start(
        self, owner_id: str, backend_revision: str, handler: SessionHandler
    ) -> SessionHandle:
        if not callable(handler):
            raise TransportError("session handler must be a host-trusted callable")
        nonce = secrets.token_hex(32)
        token = _sha256(f"{nonce}:{secrets.token_hex(32)}")
        session_id = str(uuid.uuid4())
        session = _Session(
            session_id=session_id,
            owner_id=owner_id,
            nonce_sha256=_sha256(nonce),
            ownership_token_sha256=_sha256(token),
            backend_revision=backend_revision,
            handler=handler,
        )
        receipt = self._receipt(session)
        self._schemas.validate(receipt)
        with self._lock:
            self._sessions[session_id] = session
        return SessionHandle(session_id, token, deepcopy(receipt))

    def _owned(self, session_id: str, owner_id: str, token: str) -> _Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise TransportError(f"session is unavailable: {session_id}")
        if session.owner_id != owner_id or not secrets.compare_digest(
            session.ownership_token_sha256, _sha256(token)
        ):
            raise TransportError("session ownership check failed")
        return session

    def probe(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise TransportError(f"session is unavailable: {session_id}")
            receipt = self._receipt(session)
        self._schemas.validate(receipt)
        return deepcopy(receipt)

    def resolve(
        self, session_id: str, owner_id: str, token: str
    ) -> dict[str, Any]:
        """Resolve one exact owned session without inheriting ambient sessions."""

        with self._lock:
            session = self._owned(session_id, owner_id, token)
            receipt = self._receipt(session)
        self._schemas.validate(receipt)
        return deepcopy(receipt)

    def heartbeat(
        self, session_id: str, owner_id: str, token: str, sequence: int
    ) -> dict[str, Any]:
        with self._lock:
            session = self._owned(session_id, owner_id, token)
            if session.state in {"cancelled", "closed", "failed"}:
                raise TransportError(f"cannot heartbeat a {session.state} session")
            if sequence != session.heartbeat_sequence + 1:
                raise TransportError("heartbeat sequence is stale or non-contiguous")
            session.heartbeat_sequence = sequence
            if session.state == "lost-heartbeat":
                session.state = "ready"
            return deepcopy(self._receipt(session))

    def mark_lost_heartbeat(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise TransportError(f"session is unavailable: {session_id}")
            if session.state in {"ready", "busy"}:
                session.state = "lost-heartbeat"
            return deepcopy(self._receipt(session))

    def invoke(
        self,
        session_id: str,
        owner_id: str,
        token: str,
        *,
        sequence: int,
        idempotency_key: str,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        request_sha256 = _sha256(canonical_json_bytes(request))
        if not 1 <= len(idempotency_key) <= 240:
            raise TransportError("session idempotency key is empty or over limit")
        with self._lock:
            session = self._owned(session_id, owner_id, token)
            replay = session.replies.get(idempotency_key)
            if replay is not None:
                if replay[:2] != (request_sha256, sequence):
                    raise TransportError("idempotency key was reused for different work")
                return deepcopy(replay[2])
            if session.state != "ready":
                raise TransportError(f"session is not ready: {session.state}")
            if sequence != session.last_sequence + 1:
                raise TransportError("invocation sequence is stale or non-contiguous")
            session.state = "busy"
            try:
                result = session.handler(deepcopy(dict(request)))
                if not isinstance(result, Mapping):
                    raise TransportError("session handler returned a non-object")
                reply = deepcopy(dict(result))
            except Exception:
                session.state = "failed"
                raise
            session.last_sequence = sequence
            session.replies[idempotency_key] = (request_sha256, sequence, reply)
            session.state = "ready"
            return deepcopy(reply)

    def cancel(self, session_id: str, owner_id: str, token: str) -> dict[str, Any]:
        with self._lock:
            session = self._owned(session_id, owner_id, token)
            if session.state != "closed":
                session.state = "cancelled"
                session.cleanup = "pending"
            return deepcopy(self._receipt(session))

    def collect(
        self,
        session_id: str,
        owner_id: str,
        token: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Collect one completed invocation by its exact replay identity."""

        with self._lock:
            session = self._owned(session_id, owner_id, token)
            replay = session.replies.get(idempotency_key)
            if replay is None:
                raise TransportError("session result is unavailable for collection")
            return deepcopy(replay[2])

    def close(self, session_id: str, owner_id: str, token: str) -> dict[str, Any]:
        with self._lock:
            session = self._owned(session_id, owner_id, token)
            session.state = "closed"
            session.cleanup = "complete"
            receipt = self._receipt(session)
        self._schemas.validate(receipt)
        return deepcopy(receipt)


@dataclass(slots=True)
class _Job:
    job_id: str
    payload: dict[str, Any]
    payload_sha256: str
    idempotency_key: str
    ingress_sha256: str
    state: str = "queued"
    polls: int = 0
    result: dict[str, Any] | None = None
    egress_sha256: str | None = None
    cancellation: str = "not-requested"
    orphan_state: str = "not-orphaned"
    cleanup: str = "not-started"


class DeterministicFakeScheduler:
    """A bounded scheduler model for offline job lifecycle conformance tests."""

    def __init__(
        self,
        handler: JobHandler,
        *,
        identity: str = "org.example.scheduler.fake",
        schemas: SchemaCatalog | None = None,
        max_polls: int = 10_000,
    ) -> None:
        if not callable(handler):
            raise TransportError("job handler must be a host-trusted callable")
        self.identity = identity
        self._handler = handler
        self._schemas = schemas or SchemaCatalog()
        if not 1 <= max_polls <= 1_000_000:
            raise TransportError("scheduler maximum poll count is out of bounds")
        self._max_polls = max_polls
        self._lock = RLock()
        self._jobs: dict[str, _Job] = {}
        self._keys: dict[str, str] = {}

    def _receipt(self, job: _Job) -> dict[str, Any]:
        receipt = {
            "schema": "openada.job-receipt/v0alpha1",
            "job_id": job.job_id,
            "scheduler_identity": self.identity,
            "payload_sha256": job.payload_sha256,
            "idempotency_key": job.idempotency_key,
            "state": job.state,
            "poll_count": job.polls,
            "artifacts": {
                "ingress_sha256": job.ingress_sha256,
                "egress_sha256": job.egress_sha256,
                "contained": "yes",
                "extensions": {},
            },
            "cancellation": job.cancellation,
            "orphan_state": job.orphan_state,
            "cleanup": job.cleanup,
            "extensions": {},
        }
        self._schemas.validate(receipt)
        return receipt

    def submit(
        self,
        payload: Mapping[str, Any],
        *,
        ingress_sha256: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        encoded = canonical_json_bytes(payload)
        payload_sha256 = _sha256(encoded)
        key = idempotency_key or _sha256(
            canonical_json_bytes(
                {"payload_sha256": payload_sha256, "ingress_sha256": ingress_sha256}
            )
        )
        if len(key) != 64 or any(character not in "0123456789abcdef" for character in key):
            raise TransportError("job idempotency key must be lowercase SHA-256")
        with self._lock:
            existing_id = self._keys.get(key)
            if existing_id is not None:
                existing = self._jobs[existing_id]
                if (
                    existing.payload_sha256 != payload_sha256
                    or existing.ingress_sha256 != ingress_sha256
                ):
                    raise TransportError("job idempotency key was reused for different work")
                return deepcopy(self._receipt(existing))
            job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.identity}/{key}"))
            job = _Job(
                job_id=job_id,
                payload=deepcopy(dict(payload)),
                payload_sha256=payload_sha256,
                idempotency_key=key,
                ingress_sha256=ingress_sha256,
            )
            self._jobs[job_id] = job
            self._keys[key] = job_id
            return deepcopy(self._receipt(job))

    def advance(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise TransportError(f"job is unavailable: {job_id}")
            if job.state == "queued":
                job.state = "running"
            elif job.state == "running":
                try:
                    result = self._handler(deepcopy(job.payload))
                    if not isinstance(result, Mapping):
                        raise TransportError("job handler returned a non-object")
                    job.result = deepcopy(dict(result))
                    job.egress_sha256 = _sha256(canonical_json_bytes(job.result))
                    job.state = "completed"
                except Exception:
                    job.state = "failed"
                    job.orphan_state = "unknown"
                    raise
            return deepcopy(self._receipt(job))

    def poll(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise TransportError(f"job is unavailable: {job_id}")
            if job.polls >= self._max_polls:
                raise TransportError("job polling exceeded the configured bound")
            job.polls += 1
            return deepcopy(self._receipt(job))

    def hold(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise TransportError(f"job is unavailable: {job_id}")
            if job.state not in {"queued", "running"}:
                raise TransportError(f"cannot hold a {job.state} job")
            job.state = "held"
            return deepcopy(self._receipt(job))

    def collect(self, job_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state != "completed" or job.result is None:
                raise TransportError("job result is not ready for collection")
            return deepcopy(job.result), deepcopy(self._receipt(job))

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise TransportError(f"job is unavailable: {job_id}")
            if job.state in {"queued", "running", "held"}:
                job.cancellation = "acknowledged"
                job.state = "cancelled"
                job.orphan_state = "not-orphaned"
                job.cleanup = "pending"
            elif job.state == "cancelled":
                job.cancellation = "acknowledged"
            else:
                job.cancellation = "rejected"
            return deepcopy(self._receipt(job))

    def mark_orphaned(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise TransportError(f"job is unavailable: {job_id}")
            if job.state in {"queued", "running", "held", "unknown"}:
                job.state = "unknown"
                job.orphan_state = "orphaned"
            return deepcopy(self._receipt(job))

    def reconnect(self, job_id: str, idempotency_key: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or not secrets.compare_digest(job.idempotency_key, idempotency_key):
                raise TransportError("job reconnect identity check failed")
            if job.orphan_state == "orphaned":
                job.orphan_state = "not-orphaned"
                if job.state == "unknown":
                    job.state = "running"
            return deepcopy(self._receipt(job))

    def cleanup(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise TransportError(f"job is unavailable: {job_id}")
            if job.state not in {"completed", "failed", "cancelled"}:
                raise TransportError("cannot clean up a non-terminal job")
            job.cleanup = "complete"
            return deepcopy(self._receipt(job))

    def export_state(self) -> dict[str, Any]:
        """Return bounded deterministic recovery state for an owned fake scheduler."""

        with self._lock:
            jobs = [
                {
                    "job_id": job.job_id,
                    "payload": deepcopy(job.payload),
                    "payload_sha256": job.payload_sha256,
                    "idempotency_key": job.idempotency_key,
                    "ingress_sha256": job.ingress_sha256,
                    "state": job.state,
                    "polls": job.polls,
                    "result": deepcopy(job.result),
                    "egress_sha256": job.egress_sha256,
                    "cancellation": job.cancellation,
                    "orphan_state": job.orphan_state,
                    "cleanup": job.cleanup,
                }
                for job in sorted(self._jobs.values(), key=lambda item: item.job_id)
            ]
        canonical_json_bytes(jobs)
        return {"scheduler_identity": self.identity, "jobs": jobs}

    @classmethod
    def restore(
        cls,
        handler: JobHandler,
        state: Mapping[str, Any],
        *,
        schemas: SchemaCatalog | None = None,
        max_polls: int = 10_000,
    ) -> "DeterministicFakeScheduler":
        """Restore exact fake job state and reject tamper or duplicate identities."""

        identity = state.get("scheduler_identity")
        jobs = state.get("jobs")
        if not isinstance(identity, str) or not isinstance(jobs, list):
            raise TransportError("scheduler recovery state is malformed")
        scheduler = cls(
            handler, identity=identity, schemas=schemas, max_polls=max_polls
        )
        for value in jobs:
            if not isinstance(value, Mapping):
                raise TransportError("scheduler recovery job is malformed")
            required = {
                "job_id", "payload", "payload_sha256", "idempotency_key",
                "ingress_sha256", "state", "polls", "result", "egress_sha256",
                "cancellation", "orphan_state", "cleanup",
            }
            if set(value) != required or not isinstance(value["payload"], Mapping):
                raise TransportError("scheduler recovery job has unexpected fields")
            payload = deepcopy(dict(value["payload"]))
            payload_sha256 = _sha256(canonical_json_bytes(payload))
            expected_id = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"{identity}/{value['idempotency_key']}")
            )
            result = value["result"]
            egress = (
                _sha256(canonical_json_bytes(result)) if result is not None else None
            )
            if (
                value["payload_sha256"] != payload_sha256
                or value["job_id"] != expected_id
                or value["egress_sha256"] != egress
            ):
                raise TransportError("scheduler recovery identity check failed")
            if value["job_id"] in scheduler._jobs or value["idempotency_key"] in scheduler._keys:
                raise TransportError("scheduler recovery state repeats a job identity")
            job = _Job(
                job_id=value["job_id"],
                payload=payload,
                payload_sha256=payload_sha256,
                idempotency_key=value["idempotency_key"],
                ingress_sha256=value["ingress_sha256"],
                state=value["state"],
                polls=value["polls"],
                result=deepcopy(result),
                egress_sha256=egress,
                cancellation=value["cancellation"],
                orphan_state=value["orphan_state"],
                cleanup=value["cleanup"],
            )
            scheduler._schemas.validate(scheduler._receipt(job))
            scheduler._jobs[job.job_id] = job
            scheduler._keys[job.idempotency_key] = job.job_id
        return scheduler
