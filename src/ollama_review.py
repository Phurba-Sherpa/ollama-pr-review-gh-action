import json
import os
import time

import requests
from dotenv import load_dotenv
from pydantic import ValidationError

from review import (CodeReviewResponse, format_files_for_llm,
                    generate_review_response)

load_dotenv()


SEVERITY_EMOJI = {
    "critical": "🔴❗",
    "suggestion": "💡",
    "nitpick": "🔍",
}
system_prompt = """
You are an expert code quality reviewer with deep expertise in software engineering best practices, clean code principles, and maintainable architecture. Your role is to provide thorough, constructive code reviews focused on quality, readability, and long-term maintainability.

When reviewing code, you will:

**Clean Code Analysis:**

- Evaluate naming conventions for clarity and descriptiveness
- Assess function and method sizes for single responsibility adherence
- Check for code duplication and suggest DRY improvements
- Identify overly complex logic that could be simplified
- Verify proper separation of concerns

**Error Handling & Edge Cases:**

- Identify missing error handling for potential failure points
- Evaluate the robustness of input validation
- Check for proper handling of null/undefined values
- Assess edge case coverage (empty arrays, boundary conditions, etc.)
- Verify appropriate use of try-catch blocks and error propagation

**Readability & Maintainability:**

- Evaluate code structure and organization
- Check for appropriate use of comments (avoiding over-commenting obvious code)
- Assess the clarity of control flow
- Identify magic numbers or strings that should be constants
- Verify consistent code style and formatting

**TypeScript-Specific Considerations** (when applicable):

- Prefer `type` over `interface` as per project standards
- Avoid unnecessary use of underscores for unused variables
- Ensure proper type safety and avoid `any` types when possible

**Best Practices:**

- Evaluate adherence to SOLID principles
- Check for proper use of design patterns where appropriate
- Assess performance implications of implementation choices
- Verify security considerations (input sanitization, sensitive data handling)

**Review Rules:**
- Start with a brief summary of overall code quality and 2-5 concise numbered points describing what changed in this PR.
- Organize findings by severity (critical, suggestion, nitpick)
- Provide specific examples with line references when possible
- Suggest concrete improvements with code examples
- Highlight positive aspects and good practices observed

**Review Structure:**
Provide your analysis in json format as:
{
  "reviews": [
    {
      "path": "filename",
      "body": "comment text",
      "position": 1,
      "severity": "critical|suggestion|nitpick",
      "confidence": "high|medium|low"
    }
  ],
  "summary": "Overall summary of PR"
}


Be constructive and educational in your feedback. When identifying issues, explain why they matter and how they impact code quality. Focus on teaching principles that will improve future code, not just fixing current issues.

If the code is well-written, acknowledge this and provide suggestions for potential enhancements rather than forcing criticism. Always maintain a professional, helpful tone that encourages continuous improvement.

"""

user_prompt = """
"""


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
    review_data = {
        "body": f"## 📝 Summary\n\n{review.summary}",
        "event": "COMMENT",
        "comments": [
            {
                "path": comment.path,
                "position": comment.position,
                "body": f"{SEVERITY_EMOJI[comment.severity]} **{comment.severity.upper()}**: {comment.body}",
            }
            for comment in review.reviews
            if comment.confidence == "high"
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
    api_key=None,
    custom_prompt=None,
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
            + (custom_prompt or "")
            + "\n\n**Code Changes:**\n"
            + formatted_changes
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

        try:
            formatted_review = CodeReviewResponse.model_validate_json(review_content)
        except ValidationError as e:
            print("CodeReviewValidation failed", e.errors())
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
    print(f"Ollama API KEY: {ollama_api_key}")
    print(f"GitHub API URL: {github_url}")
    print(f"GitHub Token: {github_token}")
    print(f"Owner: {owner}")
    print(f"Repo: {repo}")
    print(f"PR Number: {pr_number}")
    print(f"Custom Prompt: {custom_prompt}")
    print(f"Response Language: {response_language}")
    print(f"Model: {model}")
    print(f"Translation Model: {translation_model}")

    try:
        # Get review from Ollama
        review = request_code_review(
            github_url,
            ollama_api_url,
            github_token,
            owner,
            repo,
            pr_number,
            model,
            ollama_api_key,
            custom_prompt,
        )

        print(f"Review generated: {review}")

        # Post review back to GitHub PR
        post_review_to_github(github_url, github_token, owner, repo, pr_number, review)

    except Exception as e:
        print(f"Error during review process: {str(e)}")
        raise e
