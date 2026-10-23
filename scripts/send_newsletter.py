
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode

import bleach
import markdown
import requests


ROOT = Path.cwd()
DRAFT_DIR = (ROOT / "drafts").resolve()
STATE_FILE = ROOT / "data" / "sent_newsletters.json"

API_BASE = "https://api.resend.com"
RESEND_API_KEY = os.environ["RESEND_API_KEY"]
CONFIRMATION_SECRET = os.environ["CONFIRMATION_SECRET"]
SEGMENT_ID = os.environ["RESEND_SEGMENT_ID"]
WORKER_URL = os.environ["UNSUBSCRIBE_WORKER"].rstrip("/")
SENDER = os.environ["NEWSLETTER_SENDER"]

DRAFT_FILE = os.environ["DRAFT_FILE"].strip()
SEND_NOW = os.environ["SEND_NOW"].lower() == "true"

USER_AGENT = "bear-blog-newsletter/1.0"
REQUEST_TIMEOUT = 30

# Only allow the basic formatting needed in newsletter content.
ALLOWED_HTML_TAGS = {
    "p", "br", "hr",
    "h1", "h2", "h3", "h4",
    "strong", "em", "b", "i",
    "ul", "ol", "li",
    "blockquote", "a",
    "code", "pre",
}

ALLOWED_HTML_ATTRIBUTES = {
    "a": ["href", "title"],
}

ALLOWED_URL_PROTOCOLS = {"http", "https", "mailto"}


class TextExtractor(HTMLParser):
    """Convert rendered HTML into readable plain text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"p", "br", "hr", "h1", "h2", "h3", "h4",
                   "li", "blockquote", "pre"}:
            self.parts.append("\n")

        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in {"p", "br", "hr", "h1", "h2", "h3", "h4",
                   "li", "blockquote", "pre"}:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)

    def get_text(self):
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def resend_request(path, method="GET", payload=None, params=None,
                   extra_headers=None):
    """Make a Resend API request and return its JSON response."""

    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }

    if extra_headers:
        headers.update(extra_headers)

    try:
        response = requests.request(
            method=method,
            url=f"{API_BASE}{path}",
            headers=headers,
            json=payload,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not reach the Resend API: {exc}"
        ) from exc

    if (
        response.status_code == 403
        and "error 1010" in response.text.lower()
    ):
        raise RuntimeError(
            "Resend's Cloudflare protection blocked the request "
            "(Error 1010). Do not retry unchanged. Contact Resend "
            "support with the Cloudflare Ray ID from the response."
        )

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(
            f"HTTP {response.status_code} calling {response.url}: "
            f"{response.text[:1000]}"
        ) from exc

    if not response.content:
        return {}

    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Resend returned a non-JSON response from {response.url}."
        ) from exc


def worker_request(path, method="POST", payload=None):
    """Call the newsletter Cloudflare Worker, not the Resend API."""

    headers = {
        "Authorization": f"Bearer {CONFIRMATION_SECRET}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }

    try:
        response = requests.request(
            method=method,
            url=f"{WORKER_URL}{path}",
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not reach the unsubscribe Worker: {exc}"
        ) from exc

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(
            f"Unsubscribe Worker returned HTTP {response.status_code}: "
            f"{response.text[:500]}"
        ) from exc

    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(
            "The unsubscribe Worker returned invalid JSON."
        ) from exc


def load_draft():
    """Load and validate a Markdown draft directly inside drafts/."""

    supplied = Path(DRAFT_FILE)

    if supplied.is_absolute() or ".." in supplied.parts:
        raise RuntimeError(
            "Draft path must be relative and cannot contain '..'."
        )

    path = (ROOT / supplied).resolve()

    if path.parent != DRAFT_DIR or path.suffix.lower() != ".md":
        raise RuntimeError(
            "Choose a .md draft directly inside drafts/."
        )

    if not path.is_file():
        raise RuntimeError(f"Draft not found: {DRAFT_FILE}")

    raw = path.read_text(encoding="utf-8")

    if not raw.splitlines() or "NOT SENT" not in raw.splitlines()[0]:
        raise RuntimeError("Draft is missing its NOT SENT marker.")

    subject_match = re.search(
        r"(?m)^## Suggested subject\s*\n+([^\n]+)",
        raw,
    )
    if not subject_match:
        raise RuntimeError(
            "Could not find '## Suggested subject'."
        )

    subject = subject_match.group(1).strip()

    body_match = re.search(
        r"(?m)^## Draft\s*\n(.*)",
        raw,
        re.S,
    )
    if not body_match:
        raise RuntimeError("Could not find '## Draft'.")

    body = body_match.group(1).strip()

    # Remove the internal reminder from the bottom of the draft.
    body = re.sub(
        r"\n*>\s*Before sending:.*$",
        "",
        body,
        flags=re.S,
    ).strip()

    if not subject or not body:
        raise RuntimeError("The subject or newsletter body is empty.")

    if len(subject) > 200:
        raise RuntimeError("Subject is unexpectedly long.")

    if "\r" in subject or "\n" in subject:
        raise RuntimeError("The subject must be a single line.")

    return path, subject, body


def get_confirmed_recipients():
    """
    Read all contacts from the configured Resend segment.

    Fail closed: only contacts explicitly marked unsubscribed=False
    are eligible. Contact state must match the API response schema.
    """

    recipients = set()
    after = None

    while True:
        params = {"limit": 100}
        if after:
            params["after"] = after

        result = resend_request(
            f"/segments/{SEGMENT_ID}/contacts",
            params=params,
        )

        contacts = result.get("data")
        if not isinstance(contacts, list):
            raise RuntimeError(
                "Unexpected Resend contacts response: expected a data list."
            )

        for contact in contacts:
            if not isinstance(contact, dict):
                continue

            email = contact.get("email")
            unsubscribed = contact.get("unsubscribed")

            if (
                isinstance(email, str)
                and email.strip()
                and unsubscribed is False
            ):
                recipients.add(email.strip().lower())

        if result.get("has_more") is not True:
            break

        if not contacts or not contacts[-1].get("id"):
            raise RuntimeError(
                "Resend indicated more contacts but returned no cursor."
            )

        after = contacts[-1]["id"]

    return sorted(recipients)


def get_unsubscribe_link(email):
    result = worker_request(
        "/api/unsubscribe-link",
        payload={"email": email},
    )

    link = result.get("url")
    if not isinstance(link, str) or not link.startswith("https://"):
        raise RuntimeError(
            "Worker did not return a valid HTTPS unsubscribe URL."
        )

    return link


def make_messages(body):
    """
    Render Markdown, then sanitize its HTML.

    Raw HTML in drafts is not trusted. The allowlist prevents scripts,
    event-handler attributes, and unsafe link protocols from being sent.
    """

    rendered = markdown.markdown(
        body,
        extensions=["extra", "sane_lists"],
    )

    safe_html = bleach.clean(
        rendered,
        tags=ALLOWED_HTML_TAGS,
        attributes=ALLOWED_HTML_ATTRIBUTES,
        protocols=ALLOWED_URL_PROTOCOLS,
        strip=True,
        strip_comments=True,
    )

    return safe_html


def commit_delivery_state():
    """Commit and push delivery progress to the repository."""

    subprocess.run(
        ["git", "config", "user.name", "github-actions[bot]"],
        check=True,
    )
    subprocess.run(
        [
            "git", "config", "user.email",
            "41898282+github-actions[bot]@users.noreply.github.com",
        ],
        check=True,
    )

    relative_state_path = str(STATE_FILE.relative_to(ROOT))
    subprocess.run(
        ["git", "add", "--", relative_state_path],
        check=True,
    )

    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet"]
    )

    if diff.returncode == 0:
        return

    if diff.returncode != 1:
        raise RuntimeError(
            "Could not inspect the delivery-state changes."
        )

    subprocess.run(
        [
            "git", "commit", "-m",
            "Record newsletter delivery progress",
        ],
        check=True,
    )
    subprocess.run(["git", "push"], check=True)


def load_state():
    if not STATE_FILE.exists():
        return {"newsletters": {}}

    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Could not read delivery state file: {exc}"
        ) from exc

    if not isinstance(state, dict):
        raise RuntimeError("Delivery state must be a JSON object.")

    if not isinstance(state.get("newsletters", {}), dict):
        raise RuntimeError(
            "Delivery state has an invalid newsletters object."
        )

    state.setdefault("newsletters", {})
    return state


def main():
    if not RESEND_API_KEY or not CONFIRMATION_SECRET:
        raise RuntimeError("Required GitHub Actions secrets are missing.")

    if not SEGMENT_ID or not WORKER_URL or not SENDER:
        raise RuntimeError(
            "Required newsletter configuration is missing."
        )

    path, subject, body = load_draft()

    newsletter_id = str(path.relative_to(ROOT)).replace("\\", "/")
    content_hash = hashlib.sha256(
        (subject + "\n" + body).encode("utf-8")
    ).hexdigest()

    # Retrieve recipients before any send attempt.
    recipients = get_confirmed_recipients()

    if not recipients:
        raise RuntimeError(
            "No eligible contacts were found in the Resend segment. "
            "No email has been sent."
        )

    print(f"Draft: {newsletter_id}")
    print(f"Subject: {subject}")
    print(f"Eligible recipients: {len(recipients)}")

    if not SEND_NOW:
        print("PREVIEW ONLY: no email has been sent.")
        print(
            "Review the draft and recipient count. "
            "Run again with send_now enabled only when ready."
        )
        return

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    state = load_state()
    newsletters = state["newsletters"]
    record = newsletters.get(newsletter_id)

    if record and record.get("content_hash") != content_hash:
        raise RuntimeError(
            "This draft has changed since sending began. "
            "Use a new draft filename for a revised newsletter."
        )

    if record and record.get("status") == "sent":
        raise RuntimeError(
            "This draft is already marked sent. "
            "Refusing to send it again."
        )

    if not record:
        record = {
            "content_hash": content_hash,
            "subject": subject,
            "status": "in_progress",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "recipients": {},
        }
        newsletters[newsletter_id] = record

        STATE_FILE.write_text(
            json.dumps(state, indent=2) + "\n",
            encoding="utf-8",
        )
        commit_delivery_state()

    sent = record.setdefault("recipients", {})
    html_body = make_messages(body)

    for email in recipients:
        existing = sent.get(email)

        if existing and existing.get("status") == "sent":
            print(f"Already recorded as sent; skipping {email}")
            continue

        print(f"Preparing delivery for {email}")

        # Generate a unique unsubscribe link for each recipient.
        unsubscribe_url = get_unsubscribe_link(email)

        safe_unsubscribe_url = html.escape(
            unsubscribe_url,
            quote=True,
        )

        full_html = (
            '<div style="font-family:Arial,sans-serif;'
            'line-height:1.65;max-width:680px;margin:auto;">'
            f"{html_body}"
            "<hr style='border:0;border-top:1px solid #ddd;"
            "margin-top:32px'>"
            "<p style='font-size:13px;color:#666'>"
            "You're receiving this email because you subscribed to "
            "ᓚᘏᗢ. "
            f'<a href="{safe_unsubscribe_url}">'
            "Unsubscribe from this newsletter</a>"
            "</p></div>"
        )

        plain_extractor = TextExtractor()
        plain_extractor.feed(html_body)
        plain_body = plain_extractor.get_text()
        plain_body += (
            "\n\nYou're receiving this email because you subscribed "
            "to ᓚᘏᗢ.\nUnsubscribe: " + unsubscribe_url
        )

        # Stable idempotency key for retry protection.
        idempotency_key = hashlib.sha256(
            f"{newsletter_id}\n{email}".encode("utf-8")
        ).hexdigest()

        response = resend_request(
            "/emails",
            method="POST",
            payload={
                "from": SENDER,
                "to": [email],
                "subject": subject,
                "html": full_html,
                "text": plain_body,
            },
            extra_headers={
                "Idempotency-Key": idempotency_key,
            },
        )

        email_id = response.get("id")
        if not email_id:
            raise RuntimeError(
                f"Resend did not return an email ID for {email}. "
                "Inspect Resend before retrying."
            )

        sent[email] = {
            "status": "sent",
            "resend_email_id": email_id,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }

        record["status"] = "in_progress"
        STATE_FILE.write_text(
            json.dumps(state, indent=2) + "\n",
            encoding="utf-8",
        )

        # Save after every successful submission.
        commit_delivery_state()
        print(f"Recorded successful submission for {email}")

        time.sleep(0.2)

    record["status"] = "sent"
    record["completed_at"] = datetime.now(timezone.utc).isoformat()

    STATE_FILE.write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )
    commit_delivery_state()

    successful_count = sum(
        1
        for item in sent.values()
        if item.get("status") == "sent"
    )

    print(f"Newsletter completed: {newsletter_id}")
    print(f"Successful recipients recorded: {successful_count}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
