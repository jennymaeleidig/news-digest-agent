"""Resend email wrapper.

Per-category email building and routing. One category sends one
self-contained digest email — nothing is consolidated or skipped.

Key pieces:
  - send_digest(markdown_content, subject, recipient) sends the email to
    an explicit recipient; the sender is hardcoded to Resend's free shared
    domain (onboarding@resend.dev).
  - resolve_recipient(category) picks the category's JSON `recipient` when
    set, else falls back to the RECIPIENT_EMAIL secret.
  - digest_subject(category, date) formats the per-category subject line
    `<name> digest — <date>`.
  - empty_digest_body() is the body an empty (no-signal) category still
    sends, so a quiet category looks identical to a healthy one.

Sender is hardcoded to Resend's free shared domain (onboarding@resend.dev)
so we don't need to verify a custom domain to start. Switch to a
verified domain by changing FROM_ADDRESS once that's set up.

Errors propagate. The run seam decides what to do on failure.
"""

from __future__ import annotations

import os

import markdown as md_lib
import resend

from categories import Category

FROM_ADDRESS = "onboarding@resend.dev"


def resolve_recipient(category: Category) -> str:
    """Return the category's recipient, or the default RECIPIENT_EMAIL secret.

    The category's JSON `recipient` wins when it is set (non-null, non-empty);
    otherwise the default recipient secret applies. Raises RuntimeError if
    neither yields a recipient, matching the pre-secret-guard behavior.
    """
    if category.recipient:
        return category.recipient
    recipient = os.environ.get("RECIPIENT_EMAIL")
    if not recipient:
        raise RuntimeError("RECIPIENT_EMAIL is not set")
    return recipient


def digest_subject(category: Category, date: str) -> str:
    """Per-category subject: `<name> digest — <date>`."""
    return f"{category.name} digest — {date}"


def empty_digest_body() -> str:
    """Body for a category with no notable items today.

    Sent to the same recipient with the same subject shape so a quiet
    category is indistinguishable from an unbroken one.
    """
    return "No notable items today."


def send_digest(markdown_content: str, subject: str, recipient: str) -> str:
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        raise RuntimeError("RESEND_API_KEY is not set")
    if not recipient:
        raise RuntimeError("recipient is not set")

    resend.api_key = api_key
    html = md_lib.markdown(
        markdown_content,
        extensions=["extra"],   # tables, fenced code, footnotes
    )

    response = resend.Emails.send({
        "from": FROM_ADDRESS,
        "to": [recipient],
        "subject": subject,
        "html": html,
        "text": markdown_content,
    })
    return response["id"]
