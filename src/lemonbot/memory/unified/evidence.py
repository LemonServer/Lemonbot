"""Immutable request evidence; constructed from the exact model inputs, never reread."""

from __future__ import annotations

import hashlib
from datetime import datetime

from .archive import canonical
from .models import ArchiveModel, ConversationScope, MemoryEntry, Message, Observation, SourceRef


def fingerprint(value: ArchiveModel) -> str:
    return hashlib.sha256(canonical(value.model_dump(mode="json")).encode()).hexdigest()


class ObservationEvidence(ArchiveModel):
    observation_id: str
    source: SourceRef
    sha256: str
    expires_at: datetime | None


class MessageEvidence(ArchiveModel):
    message: Message
    source: SourceRef
    sha256: str
    observations: tuple[ObservationEvidence, ...]


class EvidenceSnapshot(ArchiveModel):
    scope: ConversationScope
    messages: tuple[MessageEvidence, ...]
    memories: tuple[MemoryEntry, ...]

    @classmethod
    def from_inputs(
        cls,
        scope: ConversationScope,
        messages: tuple[Message, ...],
        observations: tuple[Observation, ...],
        memories: tuple[MemoryEntry, ...],
    ) -> EvidenceSnapshot:
        if (
            any(m.scope != scope for m in messages)
            or len({m.message_id for m in messages}) != len(messages)
            or any(m.scope != scope or not m.active for m in memories)
            or any(o.source.scope != scope for o in observations)
            or any(
                o.source.message_id not in {m.message_id for m in messages} for o in observations
            )
        ):
            raise ValueError("snapshot inputs must be unique and in scope")
        return cls(
            scope=scope,
            memories=memories,
            messages=tuple(
                MessageEvidence(
                    message=m,
                    source=SourceRef(scope=scope, message_id=m.message_id),
                    sha256=fingerprint(m),
                    observations=tuple(
                        ObservationEvidence(
                            observation_id=o.observation_id,
                            source=o.source,
                            sha256=fingerprint(o),
                            expires_at=o.expires_at,
                        )
                        for o in sorted(observations, key=lambda o: o.observation_id)
                        if o.source.message_id == m.message_id
                    ),
                )
                for m in messages
            ),
        )

    def evidence_for(self, refs: tuple[SourceRef, ...]) -> str:
        by_id = {m.source.message_id: m for m in self.messages}
        evidence = []
        for ref in refs:
            if ref.scope != self.scope or ref.message_id not in by_id:
                raise ValueError("source not in snapshot")
            message = by_id[ref.message_id]
            observed = [
                o.sha256
                for o in message.observations
                if ref.part_id is None or o.source.part_id == ref.part_id
            ]
            # _evidence uses the same canonical payload hashing for each source.
            evidence.append(
                hashlib.sha256(
                    canonical([message.message.model_dump(mode="json"), observed]).encode()
                ).hexdigest()
            )
        return canonical(evidence)
