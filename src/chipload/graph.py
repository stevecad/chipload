"""Deterministic topological layers with one visit per dependency edge."""


class DependencyCycle(ValueError):
    def __init__(self, nodes: tuple[str, ...]):
        self.nodes = nodes
        super().__init__("dependency cycle")


def ordered_nodes(graph: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    # Missing references are reported separately by cross-object validation.
    remaining = {}
    dependents = {key: [] for key in graph}
    for key, parents in graph.items():
        dependencies = set(parents) & graph.keys()
        remaining[key] = len(dependencies)
        for parent in dependencies:
            dependents[parent].append(key)
    ready = sorted(key for key, count in remaining.items() if count == 0)
    ordered = []
    while ready:
        following = []
        for key in ready:
            ordered.append(key)
            for child in dependents[key]:
                remaining[child] -= 1
                if remaining[child] == 0:
                    following.append(child)
        ready = sorted(following)
    if len(ordered) != len(graph):
        raise DependencyCycle(tuple(sorted(key for key, count in remaining.items() if count)))
    return tuple(ordered)
