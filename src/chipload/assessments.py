"""Caller-owned assessment admission and safe, publication-fenced receipts."""

from .access import require
from .assessment_model import AssessmentRecord, AssessmentTask, AssessShop
from .lifecycle import Conflict, NotFound, ShopService, now
from .programs import ProgramService, decode, request_for
from .published import PublishedService


class AssessmentService:
    def __init__(self, repository, *, clock=now):
        self.repository, self.clock = repository, clock
        self.programs = ProgramService(repository, clock=clock)
        self.published = PublishedService(ShopService(repository), clock=clock)

    @staticmethod
    def authorize(principal, request):
        who = require(principal, request.shop_id, "evaluation:run")
        if request.private:
            require(who, request.shop_id, "evaluation:read")
        return who

    def snapshot(self, request, *, bound=True):
        with self.repository.connection() as db:
            db.execute("BEGIN")
            snapshot = self.published.snapshot_in(db, request.shop_id)
        artifact, _, _, generation, build = snapshot
        if bound and (generation, build, artifact.build_sha256) != (
            request.generation, request.build_id, request.build_sha256
        ):
            raise Conflict("Assessment publication changed")
        try:
            revision = artifact.model_sha256 if request.private else self.repository.public_catalog(artifact)["public_sha256"]
        except KeyError:
            raise NotFound("published shop not found") from None
        if revision != request.expected_revision:
            raise Conflict("Assessment publication changed")
        return snapshot

    def submit(self, principal, request):
        request = AssessShop.model_validate(request)
        who = self.authorize(principal, request)
        with self.repository.connection() as db:
            prior = db.execute(
                "SELECT * FROM python_runs WHERE shop_id=? AND actor=? AND request_key=?",
                (request.shop_id, who.subject, request.request_key),
            ).fetchone()
        if prior:
            decode(prior)
            if prior["kind"] != "assessment" or AssessShop.model_validate(
                request_for(prior).model_dump(exclude={"build_id", "build_sha256", "generation"})
            ) != request:
                raise Conflict("Request key already identifies another queued job")
            return self.read(who, request.shop_id, prior["id"])
        artifact, observations, revocations, generation, build = self.snapshot(request, bound=False)
        # Apply exactly the ordinary target/visibility policy before admission.
        self.published.evaluate_snapshot(artifact, observations, revocations, request.query,
                                         at=self.clock(), private=request.private)
        task = AssessmentTask(**request.model_dump(), build_id=build,
                              build_sha256=artifact.build_sha256, generation=generation)
        job = self.programs.enqueue(who, task, kind="assessment")
        return self.read(who, request.shop_id, job.id)

    def read(self, principal, shop, identifier):
        who = require(principal, shop, "evaluation:run")
        with self.repository.connection() as db:
            # Result and current publication/visibility share one read snapshot.
            db.execute("BEGIN")
            row = db.execute(
                "SELECT * FROM python_runs WHERE id=? AND shop_id=? AND actor=? AND kind='assessment'",
                (identifier, shop, who.subject),
            ).fetchone()
            if row is None:
                raise NotFound("Assessment not found")
            job, task = decode(row), request_for(row)
            self.authorize(who, task)
            result = AssessmentRecord(
                id=job.id, shop_id=shop, state=job.state, created_at=job.created_at,
                finished_at=job.finished_at, cancel_requested=job.cancel_requested,
                error={"failed": "Assessment failed", "interrupted": "Assessment interrupted"}.get(job.state),
            )
            if job.state != "succeeded":
                return result
            try:
                artifact, observations, revocations, generation, build = self.published.snapshot_in(db, shop)
                if (generation, build, artifact.build_sha256) != (
                    task.generation, task.build_id, task.build_sha256
                ) or (observations.revision, revocations.revision) != (
                    job.value["observation_revision"], job.value["revocation_revision"]
                ):
                    raise Conflict("Assessment snapshot changed")
            except (NotFound, Conflict, KeyError):
                return result.model_copy(update={"state": "superseded", "error": "Assessment superseded"})
            return result.model_copy(update={"evaluation": job.value["evaluation"]})

    def cancel(self, principal, shop, identifier):
        # An owner can stop work after losing private-evaluation access. This
        # response carries status only, including for already-completed jobs.
        job = self.programs.cancel(principal, shop, identifier, _assessment=True)
        return AssessmentRecord(
            id=job.id, shop_id=shop, state=job.state, created_at=job.created_at,
            finished_at=job.finished_at, cancel_requested=job.cancel_requested,
            error={"failed": "Assessment failed", "interrupted": "Assessment interrupted"}.get(job.state),
        )
