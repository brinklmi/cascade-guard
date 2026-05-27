"""Tests for Union-Find cycle detection."""

import pytest

from cascade_guard.union_find import UnionFind


class TestUnionFindBasics:
    """Basic Union-Find operations."""

    def test_make_set_creates_singleton(self):
        uf = UnionFind()
        uf.make_set("a")
        assert uf.num_nodes == 1
        assert uf.num_components == 1
        assert uf.find("a") == "a"

    def test_make_set_idempotent(self):
        uf = UnionFind()
        uf.make_set("a")
        uf.make_set("a")
        assert uf.num_nodes == 1

    def test_union_merges_components(self):
        uf = UnionFind()
        uf.make_set("a")
        uf.make_set("b")
        assert uf.num_components == 2

        merged = uf.union("a", "b")
        assert merged is True
        assert uf.num_components == 1
        assert uf.connected("a", "b")

    def test_union_same_set_returns_false(self):
        uf = UnionFind()
        uf.make_set("a")
        uf.make_set("b")
        uf.union("a", "b")

        # Already in same set
        merged = uf.union("a", "b")
        assert merged is False

    def test_find_raises_on_missing_node(self):
        uf = UnionFind()
        with pytest.raises(KeyError):
            uf.find("nonexistent")

    def test_component_size(self):
        uf = UnionFind()
        uf.make_set("a")
        uf.make_set("b")
        uf.make_set("c")
        assert uf.component_size("a") == 1

        uf.union("a", "b")
        assert uf.component_size("a") == 2
        assert uf.component_size("b") == 2

        uf.union("b", "c")
        assert uf.component_size("a") == 3


class TestCycleDetection:
    """Cycle detection via Union-Find."""

    def test_no_cycle_for_new_node(self):
        uf = UnionFind()
        uf.make_set("a")
        # Target doesn't exist yet — can't create cycle
        assert uf.would_create_cycle("a", "new") is False

    def test_cycle_detected_in_same_component(self):
        uf = UnionFind()
        uf.make_set("a")
        uf.make_set("b")
        uf.make_set("c")

        uf.union("a", "b")
        uf.union("b", "c")

        # c → a would create a cycle (all in same component)
        assert uf.would_create_cycle("c", "a") is True
        assert uf.would_create_cycle("a", "c") is True

    def test_no_cycle_across_components(self):
        uf = UnionFind()
        uf.make_set("a")
        uf.make_set("b")
        uf.make_set("c")
        uf.make_set("d")

        uf.union("a", "b")
        uf.union("c", "d")

        # a → c is fine (different components)
        assert uf.would_create_cycle("a", "c") is False

    def test_linear_chain_no_false_positives(self):
        """A → B → C → D should not trigger cycle detection for D → E."""
        uf = UnionFind()
        for node in ["A", "B", "C", "D", "E"]:
            uf.make_set(node)

        uf.union("A", "B")
        uf.union("B", "C")
        uf.union("C", "D")

        # D → E is fine (E is in a different component)
        assert uf.would_create_cycle("D", "E") is False

        # But D → A would be a cycle
        assert uf.would_create_cycle("D", "A") is True


class TestPathCompression:
    """Verify path compression works correctly."""

    def test_path_compression_maintains_correctness(self):
        uf = UnionFind()
        # Build a long chain
        nodes = [f"n{i}" for i in range(100)]
        for n in nodes:
            uf.make_set(n)

        for i in range(len(nodes) - 1):
            uf.union(nodes[i], nodes[i + 1])

        # All should be in same component
        assert uf.num_components == 1
        assert uf.component_size(nodes[0]) == 100

        # After find with path compression, all point to root
        root = uf.find(nodes[-1])
        for n in nodes:
            assert uf.find(n) == root

    def test_get_components(self):
        uf = UnionFind()
        uf.make_set("a")
        uf.make_set("b")
        uf.make_set("c")
        uf.make_set("d")

        uf.union("a", "b")
        uf.union("c", "d")

        components = uf.get_components()
        assert len(components) == 2

        # Each component has 2 members
        sizes = sorted(len(v) for v in components.values())
        assert sizes == [2, 2]
