"""Materialize private project files inside a disposable source container."""

from pathlib import Path

from .project import SourceProject


def materialize(project: SourceProject, root: Path) -> None:
    """Write a validated bundle into a fresh directory before importing any source."""
    project = SourceProject.model_validate(project)
    if root.is_symlink() or not root.is_dir() or any(root.iterdir()):
        raise ValueError("source directory must be an empty real directory")
    for entry in (*project.files, *project.assets):
        target = root / entry.path
        target.parent.mkdir(parents=True, exist_ok=True)
        content = entry.content.encode("utf-8") if hasattr(entry, "content") else entry.read_bytes()
        with target.open("xb") as stream:
            stream.write(content)
