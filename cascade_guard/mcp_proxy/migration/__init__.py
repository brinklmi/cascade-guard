"""ETRM Migration Agent Framework.

Lightweight adapters that translate legacy ETRM events into MCP tool calls,
enabling CascadeGuard to govern the migration process.

Architecture:
  Source System (RightAngle/Allegro) → Event Listener → Transform → MCP Client → CascadeGuard Proxy → Target System (Endur)
"""
