"""Optional AI grouping / classification of collected reviews.

This is a **post-processing** step. It never scrapes. It reads already-collected
reviews and adds classification fields:

* ``sentiment``  : positive | neutral | negative
* ``topic``      : bugs | pricing | onboarding | performance | login | payments |
                   support | feature_request | UX | other
* ``intent``     : bug_report | complaint | praise | feature_request | question |
                   churn_risk
* ``severity``   : low | medium | high
* ``summary``    : one-sentence summary of the review

Two backends are provided:

* ``rule_based`` (default) — deterministic keyword heuristics, no dependencies,
  no network. Good enough for the MVP and for tests.
* ``llm`` — optional hook that calls an LLM if configured. Falls back to the
  rule-based classifier when no provider/key is available.

Keeping the grouping behind a small ``classify_review`` function makes it easy
to swap in a real model later without changing the CLI.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

GROUPING_FIELDS = ["sentiment", "topic", "intent", "severity", "summary"]

# Topic -> keyword markers. Order matters: the first topic with a hit wins.
_TOPIC_KEYWORDS = [
    ("login", ["login", "log in", "sign in", "signin", "password", "authenticat", "2fa", "otp"]),
    ("payments", ["payment", "charged", "charge", "refund", "transaction", "billing", "card declined"]),
    ("pricing", ["price", "pricing", "expensive", "subscription", "cost", "too much money", "overpriced", "paywall"]),
    ("performance", ["slow", "lag", "laggy", "freeze", "freezes", "battery", "performance", "loading", "load time"]),
    ("bugs", ["bug", "crash", "crashes", "broken", "doesn't work", "does not work", "error", "glitch", "not working"]),
    ("onboarding", ["onboarding", "setup", "set up", "tutorial", "getting started", "first time", "register", "registration"]),
    ("support", ["support", "customer service", "no response", "contact", "help desk", "ticket", "unresponsive"]),
    ("feature_request", ["please add", "would be nice", "wish", "feature request", "should add", "add a", "needs a", "i want"]),
    ("UX", ["confusing", "hard to use", "interface", "ui", "ux", "design", "layout", "cluttered", "intuitive"]),
]

_POSITIVE_WORDS = [
    "love", "great", "excellent", "amazing", "awesome", "perfect", "best",
    "good", "fantastic", "wonderful", "useful", "helpful", "easy", "nice",
]
_NEGATIVE_WORDS = [
    "hate", "terrible", "awful", "worst", "bad", "useless", "horrible",
    "broken", "crash", "bug", "disappointed", "annoying", "frustrat", "scam",
    "waste", "refund", "unusable",
]
_CHURN_WORDS = ["uninstall", "deleting", "deleted", "cancel", "switching", "leaving", "won't use", "stopped using"]
_QUESTION_WORDS = ["how do i", "how to", "is there", "can i", "why does", "?"]


def _rating_of(review: Dict[str, Any]) -> int:
    try:
        return int(review.get("rating") or 0)
    except (TypeError, ValueError):
        return 0


def _text_of(review: Dict[str, Any]) -> str:
    parts = [review.get("title") or "", review.get("text") or ""]
    return " ".join(parts).lower()


def _classify_sentiment(text: str, rating: int) -> str:
    pos = sum(1 for w in _POSITIVE_WORDS if w in text)
    neg = sum(1 for w in _NEGATIVE_WORDS if w in text)
    # Ratings are a strong signal when present.
    if rating >= 4:
        pos += 1
    elif rating and rating <= 2:
        neg += 1
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


def _classify_topic(text: str) -> str:
    for topic, keywords in _TOPIC_KEYWORDS:
        if any(kw in text for kw in keywords):
            return topic
    return "other"


def _classify_intent(text: str, sentiment: str, topic: str) -> str:
    if any(w in text for w in _CHURN_WORDS):
        return "churn_risk"
    if topic == "feature_request" or "please add" in text or "wish" in text:
        return "feature_request"
    if topic == "bugs":
        return "bug_report"
    if any(w in text for w in _QUESTION_WORDS):
        return "question"
    if sentiment == "positive":
        return "praise"
    if sentiment == "negative":
        return "complaint"
    return "complaint"


def _classify_severity(rating: int, sentiment: str, topic: str) -> str:
    high_impact = {"bugs", "login", "payments"}
    if sentiment == "negative" and (rating == 1 or topic in high_impact):
        return "high"
    if sentiment == "negative":
        return "medium"
    return "low"


def _summarize(review: Dict[str, Any]) -> str:
    text = (review.get("text") or review.get("title") or "").strip()
    if not text:
        return "No review text provided."
    # First sentence, trimmed to a reasonable length.
    sentence = re.split(r"(?<=[.!?])\s+", text)[0].strip()
    if len(sentence) > 160:
        sentence = sentence[:157].rstrip() + "..."
    return sentence


def classify_review(review: Dict[str, Any], backend: str = "rule_based") -> Dict[str, Any]:
    """Return grouping fields for a single review.

    ``backend='llm'`` will use an LLM if one is configured; otherwise it falls
    back to the deterministic rule-based classifier.
    """
    if backend == "llm":
        result = _llm_classify(review)
        if result is not None:
            return result
        logger.warning("LLM backend unavailable; falling back to rule_based.")

    text = _text_of(review)
    rating = _rating_of(review)
    sentiment = _classify_sentiment(text, rating)
    topic = _classify_topic(text)
    intent = _classify_intent(text, sentiment, topic)
    severity = _classify_severity(rating, sentiment, topic)
    return {
        "sentiment": sentiment,
        "topic": topic,
        "intent": intent,
        "severity": severity,
        "summary": _summarize(review),
    }


def _llm_classify(review: Dict[str, Any]):
    """Hook for a real LLM-backed classifier.

    Returns None when no provider is configured so the caller can fall back.
    Implement this by calling your LLM of choice with a JSON-schema prompt that
    asks for the GROUPING_FIELDS. Kept as a stub to keep the MVP offline and
    deterministic by default.
    """
    return None


def classify_reviews(
    reviews: List[Dict[str, Any]], backend: str = "rule_based"
) -> List[Dict[str, Any]]:
    """Return new review dicts with grouping fields merged in."""
    out = []
    for review in reviews:
        grouped = dict(review)
        grouped.update(classify_review(review, backend=backend))
        out.append(grouped)
    return out
