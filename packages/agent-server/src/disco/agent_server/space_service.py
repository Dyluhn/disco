"""SpaceService — owner of the Spaces registry, vector store, and per-
conversation space-id selection state.

God-file decomposition (pure move, zero behavior change). The Spaces-related
state and accessors move out of ``runtime.py`` into a ``SpaceService``
collaborator constructed once in ``ConversationRuntime``:

  - ``space_store``           — ``JsonSpaceStore`` under the ProjectStore root
  - ``space_vector_store``    — lazily-cached ``DiskVectorStore`` for Space corpora
  - ``space_corpus_service``  — ``DefaultCorpusService`` with the live embedder
  - ``set_space_ids``         — pin a conversation's selected Space ids
  - ``get_space_ids``         — read a conversation's selected Space ids

The state (``_space_ids``, ``_space_vector_store_root``,
``_space_vector_store``) is owned here.  The service reaches the live
ProjectStore root and the live embedder through **narrow named typed
collaborators** — never through ``ConversationRuntime``, ``Any``, a generic
context object, or an exposed cross-domain dictionary.

Every moved method keeps a one-line delegator on ``ConversationRuntime``
because routes, ``DeepResearchService``, and tests reach them directly on the
runtime.  The primary session performs the composition wiring after inspecting
this diff.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from disco.core.llm import ConfigStore
from disco.retrieval import DefaultCorpusService, DiskVectorStore
from disco.tools.projects import ProjectStore

from .space_store import JsonSpaceStore

if TYPE_CHECKING:
    from disco.retrieval.ranking import Embedder


@runtime_checkable
class ProjectRootResolver(Protocol):
    """Resolve the current ProjectStore root path (or ``None`` when unset)."""

    def root(self) -> str | None: ...


@runtime_checkable
class EmbedderResolver(Protocol):
    """Resolve the live embedder for Space corpus ingestion."""

    def embedder(self) -> Embedder: ...


class ConfiguredProjectRoot:
    """Resolve the current ProjectStore root from persisted configuration."""

    def __init__(self, config_store: ConfigStore) -> None:
        self._config_store = config_store

    def root(self) -> str | None:
        root = ProjectStore(self._config_store.load().projects.projects_root).root
        return str(root) if root is not None else None


class SpaceService:
    """Owner of Spaces registry, vector store, and per-conversation selections.

    The state (``_space_ids``, ``_space_vector_store_root``,
    ``_space_vector_store``) is declared and owned here.  The service reaches
    the live ProjectStore root and the live embedder through the narrow typed
    collaborators ``ProjectRootResolver`` and ``EmbedderResolver`` — never
    through the runtime or a generic context object.
    """

    def __init__(self, project_root: ProjectRootResolver, embedder: EmbedderResolver) -> None:
        self._project_root = project_root
        self._embedder = embedder
        # Per-conversation selected Space ids (pinned at create/submit).
        self._space_ids: dict[str, frozenset[str]] = {}
        # Lazily-rebuilt vector store; rebuilt when the ProjectStore root changes.
        self._space_vector_store_root: str | None = None
        self._space_vector_store: DiskVectorStore | None = None

    # ---- Spaces registry + vector store -----------------------------------

    def space_store(self) -> JsonSpaceStore:
        """Public accessor for the Spaces registry under the ProjectStore root."""
        root = self._project_root.root()
        if root is None:
            return JsonSpaceStore("")
        return JsonSpaceStore(root)

    def space_vector_store(self) -> DiskVectorStore:
        """Durable vector store for Space corpora, one namespace per space_id."""
        root = self.space_store().vectors_dir
        root_str = str(root)
        if self._space_vector_store is None or self._space_vector_store_root != root_str:
            self._space_vector_store = DiskVectorStore(root)
            self._space_vector_store_root = root_str
        return self._space_vector_store

    def space_corpus_service(self) -> DefaultCorpusService:
        """Corpus service for ingesting Space documents with the live embedder."""
        return DefaultCorpusService(self.space_vector_store(), self._embedder.embedder())

    # ---- per-conversation selection ---------------------------------------

    def set_space_ids(self, conversation_id: str, space_ids: list[str] | frozenset[str]) -> None:
        clean = frozenset(str(space_id).strip() for space_id in space_ids if str(space_id).strip())
        if clean:
            self._space_ids[conversation_id] = clean
        else:
            self._space_ids.pop(conversation_id, None)

    def get_space_ids(self, conversation_id: str | None) -> frozenset[str]:
        if not conversation_id:
            return frozenset()
        return self._space_ids.get(conversation_id, frozenset())

    def validated_space_ids(
        self,
        space_ids: frozenset[str],
        *,
        owner_id: str | None,
        include_unclaimed_legacy: bool = False,
    ) -> tuple[frozenset[str], tuple[str, ...]]:
        """Partition selected ids by visibility to the conversation owner."""
        if not space_ids:
            return frozenset(), ()
        if owner_id is None:
            return frozenset(), tuple(sorted(space_ids))
        root = self._project_root.root()
        if root is None:
            return frozenset(), tuple(sorted(space_ids))
        store = JsonSpaceStore(root)
        owned: set[str] = set()
        forbidden: list[str] = []
        for space_id in sorted(space_ids):
            try:
                record = store.get(
                    space_id,
                    owner_id=owner_id,
                    include_unclaimed_legacy=include_unclaimed_legacy,
                )
            except ValueError:
                record = None
            if record is None:
                forbidden.append(space_id)
            else:
                owned.add(space_id)
        return frozenset(owned), tuple(forbidden)

    def forget(self, conversation_id: str) -> None:
        self._space_ids.pop(conversation_id, None)
