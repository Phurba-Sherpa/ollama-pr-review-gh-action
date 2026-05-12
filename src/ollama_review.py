import json
import os
import time

import requests
from dotenv import load_dotenv

from review import CodeReviewResponse, generate_review_response

load_dotenv()


system_prompt = """You are an expert code quality reviewer with deep expertise in software engineering best practices, clean code principles, and maintainable architecture. Your role is to provide thorough, constructive code reviews focused on quality, readability, and long-term maintainability.

- Focus review on changed lines only
- Use surrounding context only to understand behavior
- Do not critique unchanged code unless directly impacted

Focus on:
- correctness
- security
- maintainability
- performance
- reliability

Only report issues that are:
- actionable,
- high confidence,
- and impactful.

If uncertain, skip the issue.
Prefer precision over recall.

Only flag maintainability concerns when they create meaningful complexity or future bug risk.

Only comment on architectural concerns if they introduce clear correctness or maintainability problems.

# REVIEW FORMAT

## ✅ Summary
- Brief overview of the changes
- Overall assessment

## 🔴 Critical
- Production-breaking or security-impacting issues
- If none: `- None`

## 🟡 Important
- Real maintainability, correctness, or reliability concerns
- If none: `- None`

## 🔵 Minor
- Optional low-impact observations
- If none: `- None`

## 👍 Good Changes
- Notable improvements or good practices
- Skip if none

## 🛠 Recommendations
- Only actionable recommendations tied to findings above
- Skip generic advice

# STYLE RULES

- Keep feedback concise and scannable
- Prefer bullet points over paragraphs
- Use line references when possible
- Avoid repeating the same issue
- Avoid generic best-practice commentary
- Do not invent issues to fill sections
- It is acceptable for a PR to have no significant issues

Be professional, direct, and constructive.
Briefly explain why an issue matters and suggest a concrete fix."""

user_prompt = """
"""


def post_review_to_github(
    github_url, github_token, owner, repo, pr_number, review_body
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
    review_data = {"body": review_body, "event": "COMMENT"}

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
        changes = []
        for file in files:
            changes.append(
                {
                    "filename": file["filename"],
                    "patch": file.get("patch", ""),
                    "status": file["status"],
                }
            )

        # Convert changes to a JSON-formatted string (using indent for readability)
        changes_str = json.dumps(changes, indent=2, ensure_ascii=False)

        # Create complete prompt using the global user_prompt
        complete_user_prompt = (
            user_prompt + (custom_prompt or "") + "\n\n### CHANGES\n" + changes_str
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

        if review_content.startswith("```"):
            lines = review_content.split("\n")
            review_content = "\n".join(lines[1:-1])

        print("\n\n")
        print(review_content)
        print("\n\n")

        try:
            review_data = CodeReviewResponse.model_validate_json(review_content)
            formatted_review = generate_review_response(review_data.reviews)
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

        # Translate if needed
        if response_language.lower() != "english":
            print(
                f"Translating review to {response_language} using {translation_model}..."
            )
            review = translate_review(
                ollama_api_url,
                ollama_api_key,
                review,
                response_language,
                translation_model,
            )
            print("Translation completed.")

        # Post review back to GitHub PR
        post_review_to_github(github_url, github_token, owner, repo, pr_number, review)

    except Exception as e:
        print(f"Error during review process: {str(e)}")
        raise e
