"""Ordinary Python source for a larger, simulated discovery-load fixture."""

from datetime import datetime, timezone

from chipload import Fact, quantity
from chipload.evaluate import EvaluationRequest
from chipload.project import AcceptanceCase, SourceFile, SourceProject

SOURCE = '''from datetime import datetime, timezone
from chipload import Capability, Catalog, CatalogItem, Evidence, Fact, Process, Range, Shop, Step, Workflow, quantity

def define_shop():
    rules = tuple(Range(fact=f"dimension-{i}", minimum=quantity("0", "mm"), maximum=quantity("100", "mm")) for i in range(10))
    capability = Capability(id="offer", name="Simulated offer", process="process", visibility="public", requirements=rules, evidence=("proof",))
    return Shop(
        id="benchmark-large", name="Larger simulated shop", visibility="public",
        catalogs=(Catalog(id="inputs", name="Simulated inputs", visibility="public", items=tuple(
            CatalogItem(id=f"item-{i}", name=f"Item {i}", visibility="public", properties=(Fact(name="one", value="x" * 2000), Fact(name="two", value="y" * 2000)))
            for i in range(500)
        )),),
        processes=(Process(id="process", name="Simulated process", visibility="public"),),
        capabilities=(capability,),
        evidence=(Evidence(id="proof", name="Simulated declaration", level="declared", source="simulated", claim="Load fixture", capabilities=("offer",), requirements=rules, recorded_at=datetime(2026, 10, 20, tzinfo=timezone.utc)),),
        workflows=(Workflow(id="route", name="Repeated simulated process", visibility="public", steps=tuple(
            Step(id=f"step-{i}", capability="offer", after=(f"step-{i-1}",) if i else ()) for i in range(17)
        )),),
    )
'''


def large_project():
    request = EvaluationRequest(
        target_kind="workflow", target="route", allow_simulated=True,
        facts=tuple(Fact(name=f"dimension-{i}", value=quantity("50", "mm")) for i in range(10)),
    )
    return SourceProject(
        shop_id="benchmark-large", entrypoint="shop:define_shop",
        files=(SourceFile(path="shop.py", content=SOURCE),),
        tests=(AcceptanceCase(id="supported-route", request=request,
                              at=datetime(2026, 10, 20, tzinfo=timezone.utc), expected="supported"),),
    )


def large_workload(project):
    base = f"/v1/shops/{project.shop_id}"
    query = project.tests[0].request.model_dump(mode="json")
    return [
        {"id": "large-catalog", "action": "catalog", "arguments": {}, "method": "GET",
         "path": base + "/catalog"},
        {"id": "repeated-workflow", "action": "evaluate", "arguments": {"query": query},
         "method": "POST", "path": base + "/evaluate",
         "body": {"shop_id": project.shop_id, "query": query}, "expected": "supported"},
    ]
