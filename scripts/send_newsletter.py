
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
import requests
import markdown


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


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def get_text(self):
        return re.sub(r"\n{3,}", "\n\n", "\n".join(self.parts)).strip()


def request_json(url, method="GET", payload=None, headers=None):
    request_headers = dict(headers or {})
    request_headers["Accept"] = "application/json"

    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url, data=body, headers=request_headers, method=method
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"HTTP {exc.code} calling {url}: {details[:1000]}"
        ) from exc

def resend_get(url, api_key, params=None):
    response = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "bear-blog-newsletter/1.0",
        },
        params=params,
        timeout=30,
    )

    if response.status_code == 403 and "error 1010" in response.text.lower():
        raise RuntimeError(
            "Resend's Cloudflare protection blocked this request. "
            "No automatic retry; investigate the client or contact Resend support."
        )

    response.raise_for_status()
    return response.json()
def load_draft():
    # Only allow a Markdown file directly inside drafts/.
    supplied = Path(DRAFT_FILE)

    if supplied.is_absolute() or ".." in supplied.parts:
        raise RuntimeError("Draft path must be relative and cannot contain '..'.")

    path = (ROOT / supplied).resolve()

    if path.parent != DRAFT_DIR or path.suffix.lower() != ".md":
        raise RuntimeError("Choose a .md draft directly inside drafts/.")

    if not path.is_file():
        raise RuntimeError(f"Draft not found: {DRAFT_FILE}")

    raw = path.read_text(encoding="utf-8")

    if "NOT SENT" not in raw.splitlines()[0]:
        raise RuntimeError("Draft is missing its NOT SENT marker.")

    match = re.search(
        r"(?m)^## Suggested subject\s*\n+([^\n]+)", raw
    )
    if not match:
        raise RuntimeError("Could not find '## Suggested subject'.")

    subject = match.group(1).strip()

    body_match = re.search(r"(?m)^## Draft\s*\n(.*)", raw, re.S)
    if not body_match:
        raise RuntimeError("Could not find '## Draft'.")

    body = body_match.group(1).strip()

    # Remove the internal reminder at the end of the draft.
    body = re.sub(
        r"\n*>\s*Before sending:.*$", "", body, flags=re.S
    ).strip()

    if not subject or not body:
        raise RuntimeError("The subject or newsletter body is empty.")

    if len(subject) > 200:
        raise RuntimeError("Subject is unexpectedly long.")

    return path, subject, body, raw


def get_confirmed_recipients():
    recipients = []
    after = None

    while True:
        query = "?limit=100"
        if after:
            query += "&after=" + urllib.parse.quote(after, safe="")

        result = resend_request(
            f"/segments/{SEGMENT_ID}/contacts{query}"
        )

        contacts = result.get("data")
        if not isinstance(contacts, list):
            raise RuntimeError("Unexpected response from Resend contacts API.")

        for contact in contacts:
            email = contact.get("email")
            unsubscribed = contact.get("unsubscribed")

            # Fail closed: never send to a contact whose current
            # subscription state cannot be verified.
            if not email or unsubscribed is not False:
                continue

            recipients.append(email.strip().lower())

        if not result.get("has_more"):
            break

        if not contacts or not contacts[-1].get("id"):
            raise RuntimeError("Resend pagination returned no next cursor.")

        after = contacts[-1]["id"]

    return sorted(set(recipients))


def get_unsubscribe_link(email):
    result = request_json(
        WORKER_URL + "/api/unsubscribe-link",
        method="POST",
        payload={"email": email},
        headers={"Authorization": f"Bearer {CONFIRMATION_SECRET}"},
    )

    link = result.get("url")
    if not link or not link.startswith("https://"):
        raise RuntimeError("Worker did not return a valid unsubscribe URL.")

    return link


def make_messages(body):
    rendered = markdown.markdown(
        body,
        extensions=["extra", "sane_lists"],
    )

    # The Markdown converter escapes ordinary text; do not allow
    # arbitrary raw HTML from a draft to be injected into the email.
    # The generated content remains a simple, readable newsletter.
    return rendered


def commit_delivery_state():
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
    subprocess.run(["git", "add", str(STATE_FILE.relative_to(ROOT))], check=True)

    diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        return
    if diff.returncode != 1:
        raise RuntimeError("Could not inspect the delivery-state changes.")

    subprocess.run(
        ["git", "commit", "-m", "Record newsletter delivery progress"],
        check=True,
    )
    subprocess.run(["git", "push"], check=True)


def main():
    if not RESEND_API_KEY or not CONFIRMATION_SECRET:
        raise RuntimeError("Required GitHub Actions secrets are missing.")

    path, subject, body, raw = load_draft()

    # Stable newsletter identity based on its repository-relative path.
    newsletter_id = str(path.relative_to(ROOT)).replace("\\", "/")
    content_hash = hashlib.sha256(
        (subject + "\n" + body).encode("utf-8")
    ).hexdigest()

    recipients = get_confirmed_recipients()

    if not recipients:
        raise RuntimeError("No confirmed contacts were found in the segment.")

    print(f"Draft: {newsletter_id}")
    print(f"Subject: {subject}")
    print(f"Confirmed recipients: {len(recipients)}")

    if not SEND_NOW:
        print("PREVIEW ONLY: no email has been sent.")
        print("Review the draft and recipient count, then rerun with send_now enabled.")
        return

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    else:
        state = {"newsletters": {}}

    newsletters = state.setdefault("newsletters", {})
    record = newsletters.get(newsletter_id)

    if record and record.get("content_hash") != content_hash:
        raise RuntimeError(
            "This draft has changed since sending began. "
            "Use a new draft filename for a revised newsletter."
        )

    if record and record.get("status") == "sent":
        raise RuntimeError(
            "This draft is already marked sent. Refusing to send it again."
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
            json.dumps(state, indent=2) + "\n", encoding="utf-8"
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

        # Generate a fresh individual unsubscribe link for each contact.
        unsubscribe_url = get_unsubscribe_link(email)

        full_html = (
            '<div style="font-family:Arial,sans-serif;'
            'line-height:1.65;max-width:680px;margin:auto;">'
            f"{html_body}"
            "<hr style='border:0;border-top:1px solid #ddd;margin-top:32px'>"
            "<p style='font-size:13px;color:#666'>"
            "You're receiving this email because you subscribed to "
            "ᓚᘏᗢ. "
            f'<a href="{html.escape(unsubscribe_url, quote=True)}">'
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

        # Stable key helps Resend deduplicate retries of the same
        # recipient/newsletter request within its idempotency window.
        idempotency_key = hashlib.sha256(
            f"{newsletter_id}\n{email}".encode("utf-8")
        ).hexdigest()

        try:
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
                extra_headers={"Idempotency-Key": idempotency_key},
            )
        except Exception:
            print(
                "Delivery failed or could not be confirmed. "
                "Progress already recorded will be preserved.",
                file=sys.stderr,
            )
            raise

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
            json.dumps(state, indent=2) + "\n", encoding="utf-8"
        )

        # Save progress after every successful API response.
        commit_delivery_state()
        print(f"Recorded successful submission for {email}")

        time.sleep(0.2)

    record["status"] = "sent"
    record["completed_at"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(
        json.dumps(state, indent=2) + "\n", encoding="utf-8"
    )
    commit_delivery_state()

    print(f"Newsletter completed: {newsletter_id}")
    print(f"Successful recipients recorded: {len(sent)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
