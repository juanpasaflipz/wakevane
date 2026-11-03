"""Schema for the runner-owned Design review ledger."""

from omnigent.tools.base import Tool


class EgmReviewTool(Tool):
    @classmethod
    def name(cls):
        return "sys_egm_review"

    @classmethod
    def description(cls):
        return (
            "Freeze versioned design artifacts or compute authoritative readiness "
            "from bound reviewer results. Never submit reports yourself."
        )

    def get_schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "action": {"type": "string", "enum": ["freeze", "status"]},
                        "spec_path": {"type": "string"},
                        "contract_path": {"type": "string"},
                        "evidence_paths": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["action"],
                },
            },
        }
