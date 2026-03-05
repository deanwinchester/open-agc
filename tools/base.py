from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
import inspect

class BaseTool(BaseModel):
    """
    Base class for all tools in Open-AGC.
    """
    name: str = Field(description="The name of the tool, matching function calling schema.")
    description: str = Field(description="A clear description of what the tool does.")
    
    def get_openai_schema(self) -> Dict[str, Any]:
        """
        Generate the OpenAI format JSON schema for this tool based on its arguments.
        Must be implemented by subclasses or generated automatically.
        """
        raise NotImplementedError("Subclasses must implement get_openai_schema")

    def execute(self, **kwargs) -> Any:
        """
        Execute the tool's core logic with the provided arguments.
        """
        raise NotImplementedError("Subclasses must implement execute")
