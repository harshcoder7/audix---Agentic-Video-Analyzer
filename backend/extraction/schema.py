"""Knowledge-graph node/edge vocabulary (see plan.md §3.2)."""

NODE_TYPES = ["Step", "Decision", "System", "Actor", "DataEntity", "Screen", "Artifact"]
EDGE_TYPES = [
    "NEXT", "TRIGGERS", "USES", "DEPENDS_ON",
    "PRODUCES", "CONSUMES", "ALTERNATIVE_PATH", "PERFORMED_BY",
]

GRAPH_SCHEMA = {
    "type": "object",
    "properties": {
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "short stable kebab-case id, e.g. step-open-crm"},
                    "type": {"type": "string", "enum": NODE_TYPES},
                    "label": {"type": "string"},
                    "description": {"type": "string"},
                    "t_start_ms": {"type": "integer"},
                    "t_end_ms": {"type": "integer"},
                },
                "required": ["id", "type", "label", "t_start_ms", "t_end_ms"],
            },
        },
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                    "type": {"type": "string", "enum": EDGE_TYPES},
                },
                "required": ["source", "target", "type"],
            },
        },
    },
    "required": ["nodes", "edges"],
}
