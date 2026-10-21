"""A simulated fixture envelope with binary geometry and Python business rules."""

import json
import math
import struct
from datetime import datetime, timezone
from pathlib import Path

from chipload import (
    Capability,
    Choice,
    EvaluationRequest,
    Evidence,
    Fact,
    Process,
    PythonCheck,
    Range,
    Resource,
    Shop,
    quantity,
)
from chipload.project import AcceptanceCase, SourceAsset, SourceFile, SourceProject

AT = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
SHOP = "asset-fixture"
PRIVATE_MARKER = b"CHIPLOAD_PRIVATE_FIXTURE_ASSET"


def envelope():
    root = Path(__file__).parent / "data"
    metadata = json.loads((root / "units.json").read_text())
    if metadata != {"unit": "mm", "role": "rectangular-fixture-envelope", "source": "simulated"}:
        raise ValueError("Expected an explicitly simulated fixture envelope in millimetres")
    data = (root / "fixture.stl").read_bytes()
    if len(data) < 84:
        raise ValueError("Truncated fixture mesh")
    count = struct.unpack_from("<I", data, 80)[0]
    if not 1 <= count <= 10000 or len(data) != 84 + count * 50:
        raise ValueError("Invalid fixture mesh size")
    vertices = []
    for offset in range(84, len(data), 50):
        record = struct.unpack_from("<12fH", data, offset)
        if not all(math.isfinite(value) for value in record[:12]):
            raise ValueError("Nonfinite fixture mesh coordinate")
        vertices.extend((record[3:6], record[6:9], record[9:12]))
    sizes = tuple(max(v[axis] for v in vertices) - min(v[axis] for v in vertices) for axis in range(3))
    if any(size <= 0 for size in sizes):
        raise ValueError("Empty fixture envelope")
    # This project's mesh describes a rectangular available envelope. A bounding
    # box alone cannot prove fit inside an arbitrary fixture or qualify machining.
    return sizes


def fits_with_clearance(facts):
    if any(value is None for value in facts.values()):
        return "unknown"
    clearance = facts["clearance"].base_value()
    return "supported" if all(
        facts[axis].base_value() + 2 * clearance <= quantity(str(size), "mm").base_value()
        for axis, size in zip(("width", "depth", "height"), envelope(), strict=True)
    ) else "unsupported"


def define_shop():
    rules = tuple(Range(fact=axis, minimum=quantity("1", "mm"), maximum=quantity(str(size), "mm"))
                  for axis, size in zip(("width", "depth", "height"), envelope(), strict=True)) + (
        Range(fact="clearance", minimum=quantity("0", "mm"), maximum=quantity("5", "mm")),
        Range(fact="quantity", minimum=quantity("1", "1"), maximum=quantity("5", "1"), multiple_of=quantity("1", "1")),
        Choice(fact="arrangement", allowed=("prototype",)),
        Choice(fact="drawing-approved", allowed=(True,), on_mismatch="conditional",
               remedy="Approve the drawing before accepting the setup."),
    )
    return Shop(
        id=SHOP, name="Fixture asset example", visibility="public",
        resources=(Resource(id="fixture", name="Rectangular fixture envelope", kind="fixture"),),
        processes=(Process(id="setup", name="Prototype fixture setup", visibility="public"),),
        capabilities=(Capability(
            id="prototype-setup", name="Prototype fixture setup", visibility="public",
            process="setup", resources=("fixture",), requirements=rules, evidence=("declaration",),
            python_checks=(PythonCheck(id="clearance-fit", name="Fit with clearance", entrypoint="shop:fits_with_clearance",
                                       visibility="public", facts=("width", "depth", "height", "clearance")),),
        ),),
        evidence=(Evidence(
            id="declaration", name="Example declaration", level="declared", source="simulated",
            recorded_at=AT, claim="Simulated rectangular envelope and prototype conditions.",
            capabilities=("prototype-setup",), resources=("fixture",), requirements=tuple(
                Choice(fact=rule.fact, allowed=(True, False)) if rule.fact == "drawing-approved" else rule
                for rule in rules
            ),
        ),),
    )


def mesh(width=120):
    """Generate deterministic binary STL bytes for the example envelope."""
    vertices = tuple((x, y, z) for x in (0, width) for y in (0, 80) for z in (0, 40))
    faces = ((0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1),
             (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3))
    triangles = tuple(triangle for a, b, c, d in faces for triangle in ((a, b, c), (a, c, d)))
    return PRIVATE_MARKER.ljust(80, b"\0") + struct.pack("<I", len(triangles)) + b"".join(
        struct.pack("<12fH", 0, 0, 0, *(value for index in triangle for value in vertices[index]), 0)
        for triangle in triangles
    )


def request(**changes):
    facts = {"width": quantity("100", "mm"), "depth": quantity("60", "mm"),
             "height": quantity("20", "mm"), "clearance": quantity("2", "mm"),
             "quantity": quantity("1", "1"), "arrangement": "prototype", "drawing-approved": True} | changes
    return EvaluationRequest(target_kind="capability", target="prototype-setup", allow_simulated=True,
                             facts=tuple(Fact(name=name, value=value) for name, value in facts.items() if value is not None))


def project(width=120):
    cases = tuple(AcceptanceCase(id=name, at=AT, request=request(**changes), expected=expected)
                  for name, changes, expected in (
                      ("fits", {}, "supported"),
                      ("outside-envelope", {"width": quantity(str(width + 1), "mm")}, "unsupported"),
                      ("clearance-conflict", {"width": quantity(str(width), "mm")}, "unsupported"),
                      ("missing-clearance", {"clearance": None}, "unknown"),
                      ("business-rejection", {"arrangement": "annual-contract"}, "unsupported"),
                      ("quantity-policy", {"quantity": quantity("6", "1")}, "unsupported"),
                      ("whole-quantity", {"quantity": quantity("1.5", "1")}, "unsupported"),
                      ("drawing-review", {"drawing-approved": False}, "conditional"),
                  ))
    return SourceProject(shop_id=SHOP, entrypoint="shop:define_shop", tests=cases,
                         files=(SourceFile(path="shop.py", content=Path(__file__).read_text()),
                                SourceFile(path="data/units.json", content=json.dumps({
                                    "unit": "mm", "role": "rectangular-fixture-envelope", "source": "simulated"}))),
                         assets=(SourceAsset.from_bytes(path="data/fixture.stl", content=mesh(width)),))


if __name__ == "__main__":
    print(project().to_json().decode("utf-8"))
