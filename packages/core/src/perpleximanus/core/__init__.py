"""perpleximanus.core — the brain (no server, no UI).

Phase 0 surfaces the Event & State spine (event-state-contract.md). Later phases
add the agent loop, the real condenser, the LLM router, and security here.
"""

from __future__ import annotations

from .equality import event_content_eq
from .events import (
    SCHEMA_VERSION,
    ActionEvent,
    AgentErrorEvent,
    BaseEvent,
    CondensationEvent,
    ConversationStatus,
    ErrorEvent,
    Event,
    EventAdapter,
    EventKind,
    EventSource,
    LLMConvertible,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    PlanStep,
    SecurityRisk,
    StatusEvent,
    ToolCall,
    ToolResult,
    event_from_json_dict,
    event_to_json_dict,
)
from .migration import migrate_event
from .state import ConversationState
from .store.base import EventFilter, EventStore, Page
from .store.sqlite import DEFAULT_OWNER_ID, SqliteEventStore
from .view import (
    CondensationRequest,
    Condenser,
    LLMSummarizingCondenser,
    NoOpCondenser,
    Summarizer,
    View,
)
from .wire import WSClientFrame, WSServerFrame

__all__ = [
    "SCHEMA_VERSION",
    "ActionEvent",
    "AgentErrorEvent",
    "BaseEvent",
    "CondensationEvent",
    "CondensationRequest",
    "Condenser",
    "ConversationState",
    "ConversationStatus",
    "DEFAULT_OWNER_ID",
    "ErrorEvent",
    "Event",
    "EventAdapter",
    "EventFilter",
    "EventKind",
    "EventSource",
    "EventStore",
    "LLMConvertible",
    "LLMMessage",
    "MessageEvent",
    "MessageEvent",
    "LLMSummarizingCondenser",
    "NoOpCondenser",
    "ObservationEvent",
    "Page",
    "PlanEvent",
    "PlanStep",
    "SecurityRisk",
    "SqliteEventStore",
    "StatusEvent",
    "Summarizer",
    "ToolCall",
    "ToolResult",
    "View",
    "WSClientFrame",
    "WSServerFrame",
    "event_content_eq",
    "event_from_json_dict",
    "event_to_json_dict",
    "migrate_event",
]
