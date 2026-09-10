"""Tool (function-calling) schemas exposed to Claude during discovery."""

TOOL_DEFINITIONS = [
    {
        "name": "navigate",
        "description": "Navigate to a path within the target application (relative to its base URL).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "e.g. '/' or '/member/123/open-account'"}},
            "required": ["path"],
        },
    },
    {
        "name": "fill",
        "description": "Type a value into a text input, identified by its [index] from the latest observation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_index": {"type": "integer"},
                "value": {"type": "string"},
            },
            "required": ["element_index", "value"],
        },
    },
    {
        "name": "click",
        "description": "Click a button or link, identified by its [index] from the latest observation.",
        "input_schema": {
            "type": "object",
            "properties": {"element_index": {"type": "integer"}},
            "required": ["element_index"],
        },
    },
    {
        "name": "select_option",
        "description": "Choose an option in a dropdown, identified by its [index] from the latest observation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_index": {"type": "integer"},
                "option_label": {"type": "string"},
            },
            "required": ["element_index", "option_label"],
        },
    },
    {
        "name": "extract_table_cell",
        "description": (
            "Read a value out of a report-style table by matching the visible text of its row "
            "(e.g. row_label='Savings') and picking a cell within that row (nth_cell, default -1 "
            "= last/rightmost cell). Use this for any tabular data -- balances, IDs, statuses -- "
            "since those cells are not clickable/interactive elements."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "row_label": {"type": "string", "description": "Visible text that appears in the target row."},
                "output_name": {"type": "string", "description": "Name to store this value under."},
                "nth_cell": {"type": "integer", "description": "Cell index in the row; -1 = last cell.", "default": -1},
            },
            "required": ["row_label", "output_name"],
        },
    },
    {
        "name": "finish_success",
        "description": (
            "Call this once the goal has been fully accomplished and all requested data has been "
            "extracted. success_checkpoint_text must be exact visible text on the current page that "
            "proves you reached the right state (used as the automated success checkpoint for future replays)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"success_checkpoint_text": {"type": "string"}},
            "required": ["success_checkpoint_text"],
        },
    },
    {
        "name": "finish_business_outcome",
        "description": (
            "Call this when the application shows a legitimate, expected non-success result "
            "(e.g. 'no such member', 'access denied') rather than an error/crash."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "short slug, e.g. 'member_not_found'"},
                "description": {"type": "string"},
                "detection_text": {"type": "string", "description": "Exact visible text that identifies this outcome."},
            },
            "required": ["name", "description", "detection_text"],
        },
    },
    {
        "name": "escalate",
        "description": "Call this if you are stuck, confused, or the situation looks unsafe to proceed on your own.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]
