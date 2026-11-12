"""L1/L2/L3 graph layer materialized in Postgres.

Behind `graph_repo` API so Neo4j swap is a one-file change later.

- L1 Reality: entities, entity_aliases, memberships, relationships
- L2 Patterns: patterns
- L3 Identity: identity_nodes (goal/value/nogo/style/self)

Models live in three modules split by aggregate: `models_graph_entities`
(entities, avatars, aliases, memberships), `models_graph_relations`
(relationships, pair stats, connection suppressions, merge suggestions) and
`models_graph_layers` (patterns, identity nodes). Importing this module still
registers every graph table on `Base.metadata` and re-exports every model.
"""
from __future__ import annotations

from vera_shared.db.models_graph_entities import (
    EntityAliasRow,
    EntityAvatarRow,
    EntityRow,
    MembershipRow,
)
from vera_shared.db.models_graph_layers import IdentityNodeRow, PatternRow
from vera_shared.db.models_graph_relations import (
    ConnectionSuppressionRow,
    MergeSuggestionRow,
    PairStatsRow,
    RelationshipRow,
)

__all__ = [
    "ConnectionSuppressionRow",
    "EntityAliasRow",
    "EntityAvatarRow",
    "EntityRow",
    "IdentityNodeRow",
    "MembershipRow",
    "MergeSuggestionRow",
    "PairStatsRow",
    "PatternRow",
    "RelationshipRow",
]
