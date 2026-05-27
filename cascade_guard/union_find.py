"""Union-Find (Disjoint Set) for Cycle Detection in Delegation Graphs.

O(α(N)) amortized time per operation via path compression + union by rank.
α(N) is the inverse Ackermann function — effectively constant for all practical N.

Key insight: In a delegation graph, a cycle means an agent is attempting to
delegate to an ancestor in its own chain. Union-Find detects this instantly
by checking if source and target share a root before the edge is added.
"""

from __future__ import annotations

from typing import Optional


class UnionFind:
    """Disjoint-set forest with path compression and union by rank.

    Tracks connected components in the delegation graph.
    Cycle detection: if find(a) == find(b) before union(a, b),
    adding edge a→b would create a cycle.

    Parameters
    ----------
    None — grows dynamically as nodes are added.
    """

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}
        self._rank: dict[str, int] = {}
        self._size: dict[str, int] = {}

    @property
    def num_components(self) -> int:
        """Number of disjoint components (independent delegation trees)."""
        roots = set()
        for node in self._parent:
            roots.add(self.find(node))
        return len(roots)

    @property
    def num_nodes(self) -> int:
        """Total nodes tracked."""
        return len(self._parent)

    def make_set(self, node_id: str) -> None:
        """Create a new singleton set for a node.

        Idempotent — does nothing if node already exists.

        Parameters
        ----------
        node_id : str
            Unique identifier for the node.
        """
        if node_id not in self._parent:
            self._parent[node_id] = node_id
            self._rank[node_id] = 0
            self._size[node_id] = 1

    def find(self, node_id: str) -> str:
        """Find the root representative of the set containing node_id.

        Uses path compression for amortized O(α(N)) performance.

        Parameters
        ----------
        node_id : str
            Node to find root for.

        Returns
        -------
        str
            Root representative of the component.

        Raises
        ------
        KeyError
            If node_id has not been added via make_set.
        """
        if node_id not in self._parent:
            raise KeyError(f"Node '{node_id}' not in UnionFind. Call make_set first.")

        # Path compression: make every node on the path point directly to root
        root = node_id
        while self._parent[root] != root:
            root = self._parent[root]

        # Compress path
        current = node_id
        while current != root:
            next_node = self._parent[current]
            self._parent[current] = root
            current = next_node

        return root

    def union(self, a: str, b: str) -> bool:
        """Merge the sets containing a and b.

        Uses union by rank for balanced trees.

        Parameters
        ----------
        a : str
            First node.
        b : str
            Second node.

        Returns
        -------
        bool
            True if a merge occurred (they were in different sets).
            False if they were already in the same set (cycle detected).
        """
        root_a = self.find(a)
        root_b = self.find(b)

        if root_a == root_b:
            return False  # Already connected — cycle would be created

        # Union by rank
        if self._rank[root_a] < self._rank[root_b]:
            self._parent[root_a] = root_b
            self._size[root_b] += self._size[root_a]
        elif self._rank[root_a] > self._rank[root_b]:
            self._parent[root_b] = root_a
            self._size[root_a] += self._size[root_b]
        else:
            self._parent[root_b] = root_a
            self._size[root_a] += self._size[root_b]
            self._rank[root_a] += 1

        return True

    def connected(self, a: str, b: str) -> bool:
        """Check if two nodes are in the same component.

        Parameters
        ----------
        a : str
            First node.
        b : str
            Second node.

        Returns
        -------
        bool
            True if a and b share a root (are in the same delegation tree).
        """
        return self.find(a) == self.find(b)

    def would_create_cycle(self, source: str, target: str) -> bool:
        """Check if adding edge source→target would create a cycle.

        This is the core CascadeGuard check: before allowing a delegation,
        verify it doesn't create a circular dependency.

        Parameters
        ----------
        source : str
            Delegating agent.
        target : str
            Target agent (or new agent ID).

        Returns
        -------
        bool
            True if the edge would create a cycle (delegation should be BLOCKED).
        """
        if target not in self._parent:
            return False  # New node can't create a cycle

        return self.connected(source, target)

    def component_size(self, node_id: str) -> int:
        """Get the size of the component containing node_id.

        Parameters
        ----------
        node_id : str
            Node to check.

        Returns
        -------
        int
            Number of nodes in the same component.
        """
        root = self.find(node_id)
        return self._size[root]

    def get_components(self) -> dict[str, list[str]]:
        """Get all components as root → members mapping.

        Returns
        -------
        dict[str, list[str]]
            Mapping from root representative to list of member nodes.
        """
        components: dict[str, list[str]] = {}
        for node in self._parent:
            root = self.find(node)
            if root not in components:
                components[root] = []
            components[root].append(node)
        return components

    def reset(self) -> None:
        """Clear all state."""
        self._parent.clear()
        self._rank.clear()
        self._size.clear()
