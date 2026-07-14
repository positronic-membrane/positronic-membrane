import logging
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

class ExternalContextLoader:
    """
    Manages the safe ingestion of external files and data streams into the sandbox environment.
    Ensures external data is properly formatted for the memory orchestrator's 'sandbox:' namespace.
    """

    def __init__(self, workspace_root: Path):
        self.workspace_root = Path(workspace_root)

    def ingest_file(self, source_path: str, target_relative_path: str) -> Dict[str, Any]:
        """
        Reads a file from an external source and formats it for sandbox application.

        Args:
            source_path: The absolute or relative path to the external file.
            target_relative_path: The desired path for the file within the sandbox workspace.

        Returns:
            A dictionary containing the status, target path, content, and metadata.
        """
        source = Path(source_path)
        if not source.is_absolute():
            source = self.workspace_root / source

        if not source.exists():
            logger.warning(f"Source file not found: {source}")
            return {"status": "error", "message": f"Source file not found: {source}"}

        if not source.is_file():
            return {"status": "error", "message": f"Source path is not a file: {source_path}"}

        try:
            content = source.read_text(encoding="utf-8")
            return {
                "status": "success",
                "target_path": target_relative_path,
                "content": content,
                "size_bytes": len(content.encode("utf-8")),
                "content_type": "text/plain"
            }
        except UnicodeDecodeError:
            return {"status": "error", "message": "File is not valid UTF-8 text."}
        except Exception as e:
            logger.error(f"Error ingesting file {source_path}: {e}")
            return {"status": "error", "message": str(e)}
