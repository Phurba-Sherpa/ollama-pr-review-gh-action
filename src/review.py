import re
from pathlib import Path
from typing import List, Literal

from pydantic import BaseModel, Field

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


class Comment(BaseModel):
    path: str = Field(description="Relative path to the file.")
    body: str = Field(description="Text of the review comment.")
    position: int = Field(description="Position in the file to comment on.")
    severity: Literal["critical", "suggestion", "nitpick"]
    confidence: Literal["high", "medium", "low"]
    category: Literal["security", "performance", "quality", "standard"]


class CodeReviewResponse(BaseModel):
    reviews: List[Comment]
    summary: str = Field(
        description="concise points describing what changed. Be neutral and factual."
    )
    highlights: List[str]


def should_review(file):
    if Path(file["filename"]).suffix.lower() in IMAGE_EXTS:
        return False
    if not file.get("patch"):
        return False
    return True


def generate_review_response(file_reviews):
    """
    Generate the complete code review response combining all file reviews.

    :param file_reviews: List of FileReview objects
    :return: Formatted full review as a string
    """
    response = []

    for review in file_reviews:
        response.append(f"## {review.filename}")
        response.append(f"**Risk Score: {review.risk_score}/5**")
        response.append("")

        for feedback in review.feedback:
            response.append(f"### {feedback.title}")
            response.append(feedback.details)
            response.append("")

    return "\n".join(response)


def add_position_to_file(patch: str) -> str:
    lines = patch.splitlines()
    result = []
    position = 0
    for line in lines:
        if line.startswith("@@"):
            result.append(line)
        else:
            position += 1
            result.append(f"[pos {position}] {line}")
    return "\n".join(result)


def format_files_for_llm(files) -> str:
    result = []

    for i, file in enumerate(files, 1):
        if not should_review(file):
            continue
        result.append(f"## File {i}: {file['filename']} ({file['status']})")
        result.append(add_position_to_file(file["patch"]))
        result.append("")
    return "\n".join(result)


def extract_json(text: str) -> str:
    text = text.strip()

    # Remove fenced code block
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    return text.strip()
