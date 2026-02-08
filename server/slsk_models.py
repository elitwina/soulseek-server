from dataclasses import dataclass
from typing import Optional


@dataclass
class DownloadEvent:
	kind: str  # 'status' | 'progress' | 'finished' | 'error' | 'started' | 'files_list'
	message: str = ""
	percent: Optional[int] = None
	path: Optional[str] = None
	files_list: Optional[list[str]] = None  # List of candidate file names for client to check



