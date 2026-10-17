"""Validate untrusted selections against this turn's exact offered archive evidence."""

from __future__ import annotations

from datetime import UTC, datetime

from lemonbot.domain.models import ModelImage

from .archive import SQLiteMessageArchive
from .documents import message_units
from .images import ImageResolver
from .maintenance import UnifiedMemoryStore
from .models import (
    ContextBundle,
    ContextDecision,
    CorrectionNotice,
    Message,
    Observation,
    RetrievalResult,
    SourceRef,
)
from .remember import memory_candidate


class ContextValidationError(ValueError):
    pass


class VerifiedContextAssembler:
    def __init__(
        self,
        archive: SQLiteMessageArchive,
        images: ImageResolver | None = None,
        memories: UnifiedMemoryStore | None = None,
    ) -> None:
        self.archive, self.images, self.memories = archive, images, memories

    async def assemble(
        self,
        *,
        current: Message,
        retrieval: RetrievalResult,
        decision: ContextDecision,
        observed_image_source: SourceRef | None = None,
    ) -> ContextBundle:
        if retrieval.scope != current.scope or current.direction != "inbound":
            raise ContextValidationError("ContextScopeMismatch")
        offered = {candidate.candidate_id: candidate for candidate in retrieval.candidates}
        if any(key not in offered for key in decision.selected_candidate_ids):
            raise ContextValidationError("ContextCandidateNotOffered")
        sources = []
        memories = []
        observations: list[Observation] = (
            [] if self.memories is None else list(await self.memories.cached_content(current))
        )
        selected_refs: set[SourceRef] = set()
        corrections: dict[str, CorrectionNotice] = {}
        now = datetime.now(UTC)
        for identifier in decision.selected_candidate_ids:
            candidate = offered[identifier]
            if candidate.correction_error:
                raise ContextValidationError("ContextCorrectionsUnavailable")
            try:
                verified = (
                    ()
                    if self.memories is None
                    else await self.memories.correction_notices(
                        candidate.sources, before=current.occurred_at
                    )
                )
            except ValueError as error:
                raise ContextValidationError("ContextCorrectionsUnavailable") from error
            if verified != candidate.corrections:
                raise ContextValidationError("ContextCorrectionsChanged")
            corrections.update((n.superseded_memory_id, n) for n in verified)
            if len(corrections) > 6:
                raise ContextValidationError("ContextCorrectionsLimit")
            if candidate.memory_ids:
                if self.memories is None or len(candidate.memory_ids) != 1:
                    raise ContextValidationError("ContextMemoryInvalid")
                entry = await self.memories.get_memory(
                    scope=current.scope, memory_id=candidate.memory_ids[0]
                )
                if (
                    entry is None
                    or not entry.active
                    or memory_candidate(entry).model_copy(
                        update={"reasons": candidate.reasons, "corrections": verified}
                    )
                    != candidate
                ):
                    raise ContextValidationError("ContextMemoryUnavailable")
                if entry.created_at > current.occurred_at:
                    raise ContextValidationError("ContextMemoryInFuture")
                memories.append(entry)
                selected_refs.update(entry.sources)
                continue
            if (
                candidate.scope != current.scope
                or len(candidate.sources) != 1
                or candidate.memory_ids
            ):
                raise ContextValidationError("ContextCandidateInvalid")
            source = candidate.sources[0]
            message = await self.archive.get_message(source)
            if (
                message is None
                or not message.eligible_as_evidence
                or message.message_id == current.message_id
                or message.occurred_at > current.occurred_at
            ):
                raise ContextValidationError("ContextSourceUnavailable")
            available = await self.archive.get_observations(source)
            unit = next(
                (
                    unit
                    for unit in message_units(message, available, now)
                    if unit.unit_id == identifier
                ),
                None,
            )
            if (
                unit is None
                or unit.source != source
                or unit.text != candidate.summary
                or candidate.author_ids != (message.sender_id,)
                or candidate.occurred_at != message.occurred_at
                or candidate.observation_ids
                != (() if unit.observation_id is None else (unit.observation_id,))
            ):
                raise ContextValidationError("ContextEvidenceChanged")
            if message not in sources:
                sources.append(message)
            observations.extend(
                obs
                for obs in available
                if obs.observation_id in candidate.observation_ids and obs not in observations
            )
            selected_refs.add(source)
        image = decision.image_source
        current_images = [
            SourceRef(scope=current.scope, message_id=current.message_id, part_id=p.part_id)
            for p in current.parts
            if p.kind == "image"
        ]
        if len(current_images) > 1:
            raise ContextValidationError("ContextMultipleCurrentImages")
        if image is not None and (
            image.scope != current.scope
            or (image not in selected_refs and image not in current_images)
            or (current_images and image != current_images[0])
        ):
            raise ContextValidationError("ContextImageNotSelected")
        # A new current image is explicit input; historical images require selection.
        if image is None and len(current_images) == 1:
            image = current_images[0]
        unavailable: tuple[SourceRef, ...] = ()
        if image is not None:
            original = await self.archive.get_message(image)
            part = (
                None
                if original is None
                else next((p for p in original.parts if p.part_id == image.part_id), None)
            )
            if part is None or part.kind != "image":
                raise ContextValidationError("ContextImagePartMismatch")
            if (
                self.images is None
                or await self.images.resolve(current=current, source=image) is None
            ):
                unavailable, image = (image,), None
        decision = decision.model_copy(
            update={"image_source": image, "needs_original_image": image is not None}
        )
        if decision.current_observation:
            if observed_image_source is not None and (
                observed_image_source in current_images or observed_image_source in selected_refs
            ):
                observations.append(
                    Observation(
                        observation_id="classifier-current-image",
                        source=observed_image_source,
                        kind="image_description",
                        status="available",
                        text=decision.current_observation,
                        extractor_version="context-classifier/v1",
                        created_at=now,
                    )
                )
            else:
                decision = decision.model_copy(update={"current_observation": None})
        return ContextBundle(
            current=current,
            decision=decision,
            source_messages=tuple(sources),
            observations=tuple(observations),
            memories=tuple(memories),
            selected_sources=tuple(
                sorted(selected_refs, key=lambda s: (s.message_id, s.part_id or ""))
            ),
            corrections=tuple(corrections.values()),
            unavailable_sources=unavailable,
        )

    async def image(self, bundle: ContextBundle) -> ModelImage | None:
        source = bundle.decision.image_source
        return (
            None
            if source is None or self.images is None
            else await self.images.resolve(
                current=bundle.current,
                source=source,
            )
        )
