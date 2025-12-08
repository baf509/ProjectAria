"""
ARIA - Filesystem Tool

Phase: 3
Purpose: Built-in tool for file and directory operations

Related Spec Sections:
- Section 8.3: Phase 3 - Tools & MCP

Safety:
- Operates within user's home directory by default
- Can be configured with allowed/denied paths
- Validates all paths to prevent directory traversal
"""

import os
import pathlib
from typing import Optional
from datetime import datetime
from ..base import BaseTool, ToolParameter, ToolResult, ToolStatus, ToolType
import logging

logger = logging.getLogger(__name__)


class FilesystemTool(BaseTool):
    """
    Built-in tool for filesystem operations.

    Operations:
    - read_file: Read contents of a file
    - write_file: Write contents to a file
    - list_directory: List contents of a directory
    - create_directory: Create a new directory
    - delete_file: Delete a file
    - file_exists: Check if file/directory exists
    - get_file_info: Get file metadata
    """

    def __init__(
        self,
        allowed_paths: Optional[list[str]] = None,
        denied_paths: Optional[list[str]] = None,
    ):
        """
        Initialize filesystem tool.

        Args:
            allowed_paths: List of path prefixes that are allowed (default: user's home)
            denied_paths: List of path prefixes that are explicitly denied
        """
        super().__init__()

        # Default to user's home directory if no paths specified
        if allowed_paths is None:
            allowed_paths = [str(pathlib.Path.home())]

        self.allowed_paths = [pathlib.Path(p).resolve() for p in allowed_paths]
        self.denied_paths = [pathlib.Path(p).resolve() for p in (denied_paths or [])]

        logger.info(
            f"Initialized FilesystemTool with allowed_paths: {self.allowed_paths}"
        )

    @property
    def name(self) -> str:
        return "filesystem"

    @property
    def description(self) -> str:
        return (
            "Perform filesystem operations like reading/writing files, "
            "listing directories, and managing file metadata."
        )

    @property
    def type(self) -> ToolType:
        return ToolType.BUILTIN

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="operation",
                type="string",
                description="The filesystem operation to perform",
                required=True,
                enum=[
                    "read_file",
                    "write_file",
                    "list_directory",
                    "create_directory",
                    "delete_file",
                    "file_exists",
                    "get_file_info",
                ],
            ),
            ToolParameter(
                name="path",
                type="string",
                description="Path to the file or directory",
                required=True,
            ),
            ToolParameter(
                name="content",
                type="string",
                description="Content to write (for write_file operation)",
                required=False,
            ),
            ToolParameter(
                name="create_parents",
                type="boolean",
                description="Create parent directories if they don't exist (for write_file and create_directory)",
                required=False,
                default=False,
            ),
        ]

    def _validate_path(self, path: str) -> tuple[bool, Optional[str], Optional[pathlib.Path]]:
        """
        Validate that a path is allowed.

        Returns:
            (is_valid, error_message, resolved_path)
        """
        try:
            resolved_path = pathlib.Path(path).resolve()
        except Exception as e:
            return False, f"Invalid path: {str(e)}", None

        # Check denied paths first
        for denied in self.denied_paths:
            try:
                resolved_path.relative_to(denied)
                return False, f"Access denied: path is in denied location", None
            except ValueError:
                # Not under this denied path, continue
                pass

        # Check allowed paths
        for allowed in self.allowed_paths:
            try:
                resolved_path.relative_to(allowed)
                return True, None, resolved_path
            except ValueError:
                # Not under this allowed path, try next
                continue

        return False, f"Access denied: path is outside allowed locations", None

    async def execute(self, arguments: dict) -> ToolResult:
        """Execute the filesystem operation."""
        operation = arguments.get("operation")
        path = arguments.get("path")

        # Validate path
        is_valid, error_msg, resolved_path = self._validate_path(path)
        if not is_valid:
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=error_msg,
            )

        try:
            # Route to appropriate operation
            if operation == "read_file":
                result = await self._read_file(resolved_path)
            elif operation == "write_file":
                content = arguments.get("content", "")
                create_parents = arguments.get("create_parents", False)
                result = await self._write_file(resolved_path, content, create_parents)
            elif operation == "list_directory":
                result = await self._list_directory(resolved_path)
            elif operation == "create_directory":
                create_parents = arguments.get("create_parents", False)
                result = await self._create_directory(resolved_path, create_parents)
            elif operation == "delete_file":
                result = await self._delete_file(resolved_path)
            elif operation == "file_exists":
                result = await self._file_exists(resolved_path)
            elif operation == "get_file_info":
                result = await self._get_file_info(resolved_path)
            else:
                return ToolResult(
                    tool_name=self.name,
                    status=ToolStatus.ERROR,
                    error=f"Unknown operation: {operation}",
                )

            return result

        except Exception as e:
            logger.error(f"Filesystem operation {operation} failed: {str(e)}", exc_info=True)
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"Operation failed: {str(e)}",
            )

    async def _read_file(self, path: pathlib.Path) -> ToolResult:
        """Read contents of a file."""
        if not path.exists():
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"File not found: {path}",
            )

        if not path.is_file():
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"Path is not a file: {path}",
            )

        try:
            content = path.read_text()
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.SUCCESS,
                output=content,
                metadata={"path": str(path), "size": len(content)},
            )
        except UnicodeDecodeError:
            # Try reading as binary
            content = path.read_bytes()
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.SUCCESS,
                output=f"<binary file, {len(content)} bytes>",
                metadata={"path": str(path), "size": len(content), "binary": True},
            )

    async def _write_file(
        self,
        path: pathlib.Path,
        content: str,
        create_parents: bool,
    ) -> ToolResult:
        """Write content to a file."""
        if create_parents:
            path.parent.mkdir(parents=True, exist_ok=True)
        elif not path.parent.exists():
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"Parent directory does not exist: {path.parent}",
            )

        path.write_text(content)

        return ToolResult(
            tool_name=self.name,
            status=ToolStatus.SUCCESS,
            output=f"File written successfully: {path}",
            metadata={"path": str(path), "size": len(content)},
        )

    async def _list_directory(self, path: pathlib.Path) -> ToolResult:
        """List contents of a directory."""
        if not path.exists():
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"Directory not found: {path}",
            )

        if not path.is_dir():
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"Path is not a directory: {path}",
            )

        entries = []
        for item in sorted(path.iterdir()):
            entry = {
                "name": item.name,
                "type": "directory" if item.is_dir() else "file",
                "path": str(item),
            }

            if item.is_file():
                try:
                    entry["size"] = item.stat().st_size
                except:
                    pass

            entries.append(entry)

        return ToolResult(
            tool_name=self.name,
            status=ToolStatus.SUCCESS,
            output=entries,
            metadata={"path": str(path), "count": len(entries)},
        )

    async def _create_directory(
        self,
        path: pathlib.Path,
        create_parents: bool,
    ) -> ToolResult:
        """Create a directory."""
        if path.exists():
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"Path already exists: {path}",
            )

        path.mkdir(parents=create_parents, exist_ok=False)

        return ToolResult(
            tool_name=self.name,
            status=ToolStatus.SUCCESS,
            output=f"Directory created: {path}",
            metadata={"path": str(path)},
        )

    async def _delete_file(self, path: pathlib.Path) -> ToolResult:
        """Delete a file."""
        if not path.exists():
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"File not found: {path}",
            )

        if path.is_dir():
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"Cannot delete directory with delete_file operation: {path}",
            )

        path.unlink()

        return ToolResult(
            tool_name=self.name,
            status=ToolStatus.SUCCESS,
            output=f"File deleted: {path}",
            metadata={"path": str(path)},
        )

    async def _file_exists(self, path: pathlib.Path) -> ToolResult:
        """Check if a file or directory exists."""
        exists = path.exists()
        file_type = None

        if exists:
            if path.is_file():
                file_type = "file"
            elif path.is_dir():
                file_type = "directory"
            elif path.is_symlink():
                file_type = "symlink"

        return ToolResult(
            tool_name=self.name,
            status=ToolStatus.SUCCESS,
            output=exists,
            metadata={
                "path": str(path),
                "exists": exists,
                "type": file_type,
            },
        )

    async def _get_file_info(self, path: pathlib.Path) -> ToolResult:
        """Get file metadata."""
        if not path.exists():
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"File not found: {path}",
            )

        stat = path.stat()
        info = {
            "path": str(path),
            "name": path.name,
            "type": "directory" if path.is_dir() else "file",
            "size": stat.st_size,
            "created": datetime.fromtimestamp(stat.st_ctime).isoformat(),
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "permissions": oct(stat.st_mode)[-3:],
        }

        return ToolResult(
            tool_name=self.name,
            status=ToolStatus.SUCCESS,
            output=info,
            metadata=info,
        )
