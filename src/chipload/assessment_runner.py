"""Trusted assessment execution; only bounded check answers cross back from source."""

from .artifact import canonical_json, digest
from .assessment_model import AssessmentWork, CheckCall
from .assessments import AssessmentService
from .containers import remove_containers
from .limits import bounded_json
from .python_checks import CaseChecks, PythonEvaluation, check_plans, validated_outcomes
from .sandbox import DockerBuilder, SandboxError, exchange


class DockerAssessmentRunner:
    def __init__(self, environment):
        self.builder = DockerBuilder(environment)

    def run(self, identifier, work, *, seconds, guard, keep_running):
        payload = canonical_json(work)
        bounded_json(payload)  # Bound the complete source/query envelope before Docker.
        name = "chipload-code-" + identifier
        command = self.builder.create_command(name)
        command[-1] = "chipload.assessment_worker"
        try:
            container = guard.create(command)
            code, output, _ = exchange(
                ["docker", "start", "--attach", "--interactive", container], payload,
                seconds=seconds, keep_running=keep_running,
            )
            if code != 0:
                raise SandboxError("Assessment worker failed")
            record = CaseChecks.model_validate(bounded_json(output))
            if record.id != "assessment":
                raise SandboxError("Invalid assessment worker response")
            return record.results
        finally:
            remove_containers(name, guard=guard)


class AssessmentProcessor:
    def __init__(self, repository, runner, *, clock):
        self.repository, self.runner, self.clock = repository, runner, clock
        self.service = AssessmentService(repository, clock=clock)

    def execute(self, row, task, *, guard, keep_running):
        artifact, observations, revocations, _, _ = self.service.snapshot(task)
        preliminary = self.service.published.evaluate_snapshot(
            artifact, observations, revocations, task.query, at=self.clock(), private=task.private
        )
        opaque = any(r["code"] == "unpublished-condition" for r in preliminary["reasons"])
        plan = {} if opaque else check_plans(artifact.shop).get((task.query.target_kind, task.query.target), {})
        answers = None
        if plan:
            env = self.runner.builder.environment
            if (artifact.identity.sdk_sha256, artifact.identity.dependencies_sha256, artifact.identity.runtime) != (
                env.sdk_sha256, env.dependencies_sha256, f"CPython-{env.python_version};docker-{env.image}"
            ):
                raise SandboxError("Published build needs its original worker environment")
            with self.repository.connection() as db:
                source = self.repository.source(db, task.shop_id, artifact.identity.source_sha256).project
            work = AssessmentWork(
                project=source, query=task.query, model_sha256=artifact.model_sha256,
                checks=tuple(CheckCall(capability=cap, check=check) for (cap, _), check in sorted(plan.items())),
            )
            answers = self.runner.run(row["id"], work, seconds=task.seconds,
                                      guard=guard, keep_running=keep_running)
        if not keep_running():
            raise SandboxError("Assessment is no longer active")
        # Select current publication, observations and revocations after execution.
        artifact, observations, revocations, _, _ = self.service.snapshot(task)
        at = self.clock()
        context = None if answers is None else PythonEvaluation(
            build_sha256=artifact.build_sha256, request_sha256=digest(canonical_json(task.query)),
            evaluated_at=at, results=answers,
        )
        validated_outcomes(artifact, task.query, at, context)
        evaluation = self.service.published.evaluate_snapshot(
            artifact, observations, revocations, task.query, at=at,
            private=task.private, python_evaluation=context,
        )
        return {"value": {"evaluation": evaluation, "observation_revision": observations.revision,
                          "revocation_revision": revocations.revision}}
