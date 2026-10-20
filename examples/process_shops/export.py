"""Export each example as a self-contained, multi-module Python source project."""

from pathlib import Path

from chipload.project import SourceFile, SourceProject

from . import mixed
from .library import BY_ID
from .scenarios import scenarios


def project(id: str) -> SourceProject:
    if id == "mixed":
        shop_id = mixed.SHOP_ID
        code = "from process_shops.mixed import define_shop\n"
        cases = mixed.scenarios()
    else:
        example = BY_ID[id]
        shop_id = example.shop_id
        code = (
            "from process_shops.library import BY_ID\n"
            "from process_shops.shops import define_shop as compose\n"
            f"def define_shop():\n    return compose(BY_ID[{id!r}])\n"
        )
        cases = scenarios(example)
    # The CLI imports the repository's trusted-local runner. It is not needed in
    # an isolated project. Every imported authoring component is supplied here.
    files = tuple(
        SourceFile(path=f"process_shops/{file.name}", content=file.read_text())
        for file in sorted(Path(__file__).parent.glob("*.py"))
        if file.name not in {"__main__.py", "export.py"}
    )
    return SourceProject(
        shop_id=shop_id,
        entrypoint="shop:define_shop",
        files=(SourceFile(path="shop.py", content=code), *files),
        tests=cases,
    )
