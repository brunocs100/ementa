"""Agent memory.

Short-term memory is the LangGraph checkpointer: one thread per conversation. Long-term memory is
the LangGraph store on Postgres with a local vector index, a writer that learns from finished
turns without showing personal data to the model, and forgetting by data subject (LGPD, art. 18).

```python
store = open_postgres_store(database_url, embedder)  # once per process
writer.write(turn, targets=[Namespace("lawyer", user_id)], mapping=conversation_mapping)
memories = search_memories(store, [Namespace("lawyer", user_id)], question)
forget_subject(store, subject_id(key, "CPF", "123.456.789-09"))  # how many were deleted
```

Episodic memory (F4) keeps the drafts lawyers approved or edited, pseudonymized, as few-shot
examples: `Episodes(store, guard, subject_key=key).examples_for(agent, query, k)`.
"""

from ementa_kit.memory.checkpoint import CHECKPOINT_SCHEMA, postgres_checkpointer
from ementa_kit.memory.embeddings import LocalEmbeddings
from ementa_kit.memory.episodes import EPISODES, Episode, Episodes, edit_diff, episodes_of
from ementa_kit.memory.store import (
    SCHEMA,
    SUBJECT_ID_PATTERN,
    Memory,
    Namespace,
    forget_subject,
    memory_value,
    open_postgres_store,
    search_memories,
    subject_id,
)
from ementa_kit.memory.writer import MAX_MEMORIES, Candidate, MemoryWriter, Written

__all__ = [
    "CHECKPOINT_SCHEMA",
    "EPISODES",
    "MAX_MEMORIES",
    "SCHEMA",
    "SUBJECT_ID_PATTERN",
    "Candidate",
    "Episode",
    "Episodes",
    "LocalEmbeddings",
    "Memory",
    "MemoryWriter",
    "Namespace",
    "Written",
    "edit_diff",
    "episodes_of",
    "forget_subject",
    "memory_value",
    "open_postgres_store",
    "postgres_checkpointer",
    "search_memories",
    "subject_id",
]
