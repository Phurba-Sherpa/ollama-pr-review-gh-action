import json
import os
import re
import sys
import time
from collections import Counter

import requests
from dotenv import load_dotenv
from pydantic import ValidationError

from config import FileConfig
from review import (CodeReviewResponse, extract_json, format_files_for_llm,
                    generate_review_response)

load_dotenv()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


SEVERITY_EMOJI = {
    "critical": "🔴❗",
    "suggestion": "💡",
    "nitpick": "🔍",
}
system_prompt = """
# PR Review Agent — System Prompt

You are an elite code reviewer combining deep expertise in **code quality**, **performance optimization**, and **application security**. Your role is to provide thorough, inline PR reviews that are constructive, specific, and grounded in the project's own standards.

You will receive:
1. A git diff of the PR changes
2. The contents of `PROJECT_STANDARD.md` (project conventions, architecture, tech stack, naming rules)

---

## Review Methodology

Work through the diff in this order:

1. **Understand the change** — identify what the PR is doing before critiquing it
2. **Map the attack surface and data flows** — trace untrusted inputs to sensitive operations
3. **Assess algorithmic and I/O efficiency** — flag complexity issues and resource waste
4. **Evaluate code structure and maintainability** — clean code, naming, separation of concerns
5. **Cross-reference project standards** — flag any deviation from `PROJECT_STANDARD.md`

---

## What to Review

### 🔴 Security
- OWASP Top 10: injection (SQL, NoSQL, command), broken auth, sensitive data exposure, XSS, CSRF, IDOR, broken access control, security misconfiguration
- Input validation: all external inputs must be validated server-side; client-side is supplementary only
- Authentication & sessions: secure cookie flags, proper timeouts, session invalidation, modern password hashing (bcrypt, Argon2, PBKDF2)
- Authorization: verify checks exist at every protected resource; look for privilege escalation paths
- Cryptography: flag weak algorithms, hardcoded secrets, improper key management
- File operations: path traversal, missing type/size/content validation on uploads
- Race conditions and TOCTOU vulnerabilities

### 🟠 Performance
- Algorithmic complexity: flag O(n²) or worse where avoidable
- N+1 query problems and missing indexes
- Unnecessary re-computation, redundant API calls, missing memoization or caching
- Blocking synchronous operations that should be async
- Memory leaks: unclosed connections, unremoved event listeners, circular references
- Large object allocations inside loops
- Missing pagination, projection, or filtering on data fetches
- Retry storms from improper error handling on network calls

### 🟡 Code Quality
- Naming: clarity, descriptiveness, adherence to project conventions in `PROJECT_STANDARD.md`
- Single Responsibility: functions/methods doing more than one thing
- DRY: duplicated logic that should be extracted
- Error handling: missing try/catch, unhandled promise rejections, swallowed errors, missing null/undefined guards
- Edge cases: empty arrays, boundary values, unexpected types
- Magic numbers/strings: should be named constants
- Over-engineering or under-engineering for the context
- SOLID principles where applicable
- Comments: flag missing explanations on non-obvious logic; flag over-comments on obvious code
- Consistent formatting and style (cross-reference project standard)

---

## Project Standard Adherence

**Always** check the diff against `PROJECT_STANDARD.md` for:
- Folder structure violations
- Naming convention deviations (variables, functions, classes, files)
- Disallowed patterns or libraries
- Required architectural patterns not followed
- Tech-stack-specific rules (e.g. TypeScript: prefer `type` over `interface`, no `any`, no unused variables)

If `PROJECT_STANDARD.md` is not provided, note this and apply general best practices only.

---

## Severity Definitions

| Severity | Meaning |
|---|---|
| `critical` | Must fix before merge. Security vulnerability, data loss risk, or production breakage. |
| `suggestion` | Should fix. Meaningful impact on quality, performance, or maintainability. |
| `nitpick` | Optional. Minor style, naming, or convention issue. Low effort, low risk. |

---

## Output Format

Return **only** valid JSON. No markdown, no preamble, no explanation outside the JSON.

```json
{
  "summary": "2–4 sentence overview of what this PR does and its overall quality. Call out any systemic patterns (good or bad).",
  "highlights": [
    "1–5 short bullet strings noting what changed in this PR"
  ],
  "reviews": [
    {
      "path": "relative/path/to/file.ts",
      "position": 12,
      "severity": "critical | suggestion | nitpick",
      "category": "security | performance | quality | standard",
      "confidence": "high | medium | low",
      "body": "Concise description of the issue. Explain WHY it matters and what the impact is. Include a concrete fix or code example where it adds clarity."
    }
  ]
}
```

### Field notes
- `position`: the line number in the **diff** (not the file) where the comment should be anchored. Use the first relevant line if the issue spans multiple lines.
- `body`: be specific — reference the actual variable names, function names, or patterns in the diff. Generic comments ("this could be improved") are not acceptable.
- `confidence: low` — use when the issue depends on context not visible in the diff (e.g. "if this is called from untrusted input..."). Still flag it; just be honest about uncertainty.
- If no issues are found in a category, omit those entries. Do not invent issues.
- If the code is well-written, say so clearly in `summary`. Forced criticism is worse than no criticism.

---

## Tone

- Constructive and educational — explain *why*, not just *what*
- Specific — reference actual code, not abstractions
- Honest — if something is good, acknowledge it
- Professional — no sarcasm, no condescension
- Proportional — a one-line nitpick doesn't need three paragraphs

"""

user_prompt = """
"""


def mask_secret(value):
    if not value:
        return "<unset>"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def post_review_to_github(
    github_url, github_token, owner, repo, pr_number, review: CodeReviewResponse
):
    """
    Post a review comment to a GitHub PR.
    :param github_token: GitHub token for authentication
    :param repo: repo name
    :param pr_number: PR number
    :param review_body: review body text
    :return:
    """
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    review_url = f"{github_url}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    inline_comments = [c for c in review.reviews if c.confidence in ("high", "medium")]
    low_confidence = [c for c in review.reviews if c.confidence == "low"]

    # Preserving low confidence reviews
    low_conf_section = ""
    if low_confidence:
        items = "\n".join(f"- `{c.path}`: {c.body}" for c in low_confidence)
        low_conf_section = f"\n\n**⚠️ Needs context (not posted inline):**\n{items}"

    hl_sec = ""
    if review.highlights:
        items = "\n".join(f"- {h}" for h in review.highlights)
        hl_sec = f"\n\n**📌 Highlights:**\n{items}"

    category_counts = Counter(c.category for c in review.reviews)
    category_breakdown = " . ".join(
        f"`{cat}`: {count}" for cat, count in sorted(category_counts.items())
    )

    review_data = {
        "body": f"## 📝 PR Review\n\n{review.summary}{hl_sec}\n\n**Findings:** {category_breakdown}{low_conf_section}",
        "event": "COMMENT",
        "comments": [
            {
                "path": comment.path,
                "position": comment.position,
                "body": (
                    f"{SEVERITY_EMOJI[comment.severity]} **{comment.severity.upper()}** `{comment.category}`"
                    f"{' *(context-dependent)*' if comment.confidence == 'medium' else ''}: {comment.body}"
                ),
            }
            for comment in inline_comments
        ],
    }

    response = requests.post(review_url, headers=headers, json=review_data)
    response.raise_for_status()
    return response.json()


def manage_ollama_model(api_url, api_key, model_name, action):
    """
    Manage Ollama model (pull, load, unload)
    """
    endpoint = f"{api_url}/api/generate"

    # Setup Authorization header
    headers = {}
    if "ollama.com" in (api_url or ""):
        if not api_key:
            raise ValueError(f"OLLAMA_API_KEY required for endpoint ollama.com")
        headers["Authorization"] = f"Bearer {api_key}"

    if action == "load":
        request_data = {"model": model_name}
    elif action == "unload":
        request_data = {"model": model_name, "keep_alive": 0}
    else:  # pull
        endpoint = f"{api_url}/api/pull"
        request_data = {"name": model_name}

    print(f"Attempting to {action} model {model_name}...")
    try:
        response = requests.post(
            endpoint, headers=headers, json=request_data, stream=(action == "pull")
        )
        response.raise_for_status()

        if action == "pull":
            for line in response.iter_lines():
                if line:
                    status = json.loads(line)
                    if "status" in status:
                        print(f"Model {model_name}: {status['status']}")
                    if "error" in status:
                        raise Exception(f"Error pulling model: {status['error']}")
        else:
            result = response.json()
            if result.get("error"):
                raise Exception(f"Error during model {action}: {result['error']}")

        print(f"Successfully {action}ed model {model_name}")
        return True
    except Exception as e:
        print(f"Error during model {action}: {str(e)}")
        return False


def prepare_model(api_url, api_key, model_name):
    """
    Prepare model for use (pull and load)
    """
    if "ollama.com" not in (api_url or ""):
        if not manage_ollama_model(api_url, api_key, model_name, "pull"):
            raise Exception(f"Failed to pull model: {model_name}")
        time.sleep(2)

    if not manage_ollama_model(api_url, api_key, model_name, "load"):
        raise Exception(f"Failed to load model: {model_name}")
    time.sleep(3)


def cleanup_model(api_url, api_key, model_name):
    """
    Cleanup model after use (unload)
    """
    manage_ollama_model(api_url, api_key, model_name, "unload")
    time.sleep(1)


def translate_review(api_url, api_key, review_text, target_language, translation_model):
    """
    Translate the review text using specified model
    """
    try:
        # Prepare translation model
        prepare_model(api_url, api_key, translation_model)

        translation_prompt = f"""
Please translate the following code review into {target_language}. 
Maintain the technical terminology in English where appropriate.
Well-known terms can be left untranslated:
- Mocking, API, Database, Cache, Error handling,
- Unit test, Integration test, System test, End-to-end test, etc.
You must not translate the code snippets or filenames in the review and should keep them in English. 
You must not add or remove any information from the review.
Review to translate:
{review_text}
"""
        print("Translation Prompt given to Ollama:", translation_prompt)
        headers = {}

        if "ollama.com" in (api_url or ""):
            if not api_key:
                raise ValueError(f"OLLAMA_API_KEY required for ollama.com endpoint")
            headers["Authorization"] = f"Bearer {api_key}"

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        translation_request = {
            "model": translation_model,
            "prompt": translation_prompt,
            "stream": False,
        }

        translation_response = requests.post(
            f"{api_url}/api/generate", headers=headers, json=translation_request
        )
        translation_response.raise_for_status()
        translation = translation_response.json()

        print("Translation Response:", translation)

        return translation["response"] if "response" in translation else translation
    finally:
        # Cleanup translation model
        cleanup_model(api_url, api_key, translation_model)


def request_code_review(
    github_url,
    api_url,
    github_token,
    owner,
    repo,
    pr_number,
    model,
    review_config,
    api_key=None,
):
    try:
        # Prepare review model
        prepare_model(api_url, api_key, model)

        headers = {
            "Authorization": f"token {github_token}",
            "Accept": "application/vnd.github.v3+json",
        }

        # Complete system prompt with response language
        complete_system_prompt = f"{system_prompt}."
        print("Complete System Prompt given to Ollama:", complete_system_prompt)
        # Get the PR files
        pr_url = f"{github_url}/repos/{owner}/{repo}/pulls/{pr_number}/files"
        response = requests.get(pr_url, headers=headers)
        response.raise_for_status()
        files = response.json()

        # Collect all changed code
        # Convert changes to a JSON-formatted string (using indent for readability)
        formatted_changes = format_files_for_llm(files)

        # Create complete prompt using the global user_prompt
        complete_user_prompt = (
            user_prompt
            + "\n\n**Code Changes:**\n"
            + "<diff>\n"
            + formatted_changes
            + "\n</diff>"
            + "\n\n**Project Standard:**\n"
            + "<project_standard>\n"
            + (review_config or "")
            + "\n</project_standard>"
        )
        print("Complete User Prompt given to Ollama:", complete_user_prompt)

        # Require Ollama API Key for cloud model
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

        # Request code review from Ollama
        review_request = {
            "model": model,  # You might want to make this configurable
            "system": complete_system_prompt,
            "prompt": complete_user_prompt,
            "stream": False,
            "format": CodeReviewResponse.model_json_schema(),
        }

        review_response = requests.post(
            f"{api_url}/api/generate", headers=headers, json=review_request
        )
        review_response.raise_for_status()
        review_json = review_response.json()

        # Parse structured response
        review_content = (
            review_json["response"] if "response" in review_json else review_json
        )

        # Remove markdown code fences if present
        if isinstance(review_content, str):
            review_content = extract_json(review_content)

        try:
            formatted_review = CodeReviewResponse.model_validate_json(review_content)
        except ValidationError:
            try:
                data = json.loads(review_content)
                formatted_review = CodeReviewResponse.model_validate(data)
            except Exception:
                formatted_review = review_content
        return formatted_review
    finally:
        # Cleanup review model
        cleanup_model(api_url, api_key, model)


if __name__ == "__main__":
    # Get input arguments from environment variables
    ollama_api_url = os.getenv("OLLAMA_API_URL")
    ollama_api_key = os.getenv("OLLAMA_API_KEY")
    github_token = os.getenv("MY_GITHUB_TOKEN")
    owner = os.getenv("OWNER")
    repo = os.getenv("REPO")
    pr_number = os.getenv("PR_NUMBER")
    custom_prompt = os.getenv("CUSTOM_PROMPT")
    response_language = os.getenv("RESPONSE_LANGUAGE", "english")
    model = os.getenv("MODEL", "qwen3-coder:480b-cloud")
    translation_model = os.getenv(
        "TRANSLATION_MODEL", "exaone3.5:32b"
    )  # Add translation model
    github_url = os.getenv("GITHUB_API_BASE_URL", "https://api.github.com")

    print(f"Ollama API URL: {ollama_api_url}")
    print(f"Ollama API KEY: {mask_secret(ollama_api_key)}")
    print(f"GitHub API URL: {github_url}")
    print(f"GitHub Token: {mask_secret(github_token)}")
    print(f"Owner: {owner}")
    print(f"Repo: {repo}")
    print(f"PR Number: {pr_number}")
    print(f"Custom Prompt: {custom_prompt}")
    print(f"Response Language: {response_language}")
    print(f"Model: {model}")
    print(f"Translation Model: {translation_model}")

    try:
        review_config = FileConfig(custom_prompt).load()

        # Get review from Ollama
        review = request_code_review(
            github_url,
            ollama_api_url,
            github_token,
            owner,
            repo,
            pr_number,
            model,
            review_config,
            ollama_api_key,
        )

        print(f"Review generated: {review}")

        # Post review back to GitHub PR
        post_review_to_github(github_url, github_token, owner, repo, pr_number, review)

    except Exception as e:
        print(f"Error during review process: {str(e)}")
        raise e
