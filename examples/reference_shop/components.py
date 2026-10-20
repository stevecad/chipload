"""Reusable components; all manufacturing envelopes are fictional test inputs."""

from chipload import Capability, Choice, ObservationRequirement, Range, quantity


def envelope(x: str, y: str, z: str) -> tuple[Range, ...]:
    return tuple(
        Range(fact=axis, minimum=quantity("0.001", "mm"), maximum=quantity(limit, "mm"))
        for axis, limit in zip(("length", "width", "height"), (x, y, z), strict=True)
    )


def mill(*, capability_id: str, machine: str, fixture: str, length: str, width: str) -> Capability:
    """Compose a milling offer from a machine, a fixture, and shared tooling."""
    from chipload import Fact

    return Capability(
        id=capability_id,
        name=f"Aluminium milling on {machine}",
        visibility="public",
        description="Fictional setup envelope; geometry review and the stated fixture are required.",
        process="milling",
        resources=(machine, "shared-endmill", fixture, "stock-al6061"),
        requirements=(
            Choice(fact="material", allowed=("al-6061-t6",), catalog="materials"),
            *envelope(length, width, "80"),
            Range(fact="tolerance", minimum=quantity("0.05", "mm")),
            Choice(fact="geometry-reviewed", allowed=(True,), on_mismatch="unknown"),
            Choice(
                fact="fixture",
                allowed=(fixture,),
                on_missing="conditional",
                on_mismatch="conditional",
                remedy=f"Set up and verify the {fixture} fixture.",
            ),
        ),
        evidence=(f"{capability_id}-declaration",),
        observations=(
            ObservationRequirement(
                observation=f"{machine}-available",
                resource=machine,
                property="available",
                purpose="readiness",
                allowed=(True,),
                remedy="Schedule the work when this machine becomes available.",
            ),
            ObservationRequirement(
                observation="shared-endmill-available",
                resource="shared-endmill",
                property="available",
                purpose="readiness",
                allowed=(True,),
                remedy="Reserve the shared cutter before scheduling.",
            ),
            ObservationRequirement(
                observation="aluminium-stock-available",
                resource="stock-al6061",
                property="stock-available",
                purpose="readiness",
                allowed=(True,),
                remedy="Confirm suitable stock is available before scheduling.",
            ),
        ),
        output_state=(Fact(name="geometry", value="machined"), Fact(name="surface", value="bare")),
    )
