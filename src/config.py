from pathlib import Path
import os


class FileConfig:
    def __init__(self, prompt: str | None = None):
        self.prompt = prompt if prompt is not None else os.getenv("CUSTOM_PROMPT", "")

    def load(self) -> str:
        prompt = self.prompt.strip()

        if not prompt:
            return ""

        path = Path(prompt)

        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8").strip()

        # If file does not exist, treat it as raw prompt text
        return prompt