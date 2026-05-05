"""WeCom template card builder."""
from __future__ import annotations


def build_text_notice_card(title: str, description: str = "", source: str = "") -> dict:
    """Build a simple text notice template card."""
    card: dict = {
        "card_type": "text_notice",
        "source": {"desc": source or "SuperCC"},
        "main_title": {"title": title, "desc": description},
    }
    return card


def build_news_notice_card(title: str, description: str = "", url: str = "", source: str = "") -> dict:
    """Build a news notice template card."""
    card: dict = {
        "card_type": "news_notice",
        "source": {"desc": source or "SuperCC"},
        "main_title": {"title": title, "desc": description},
    }
    if url:
        card["card_action"] = {"type": 1, "url": url}
    return card


def build_button_interaction_card(
    title: str,
    description: str = "",
    buttons: list[dict] | None = None,
    source: str = "",
) -> dict:
    """Build a button interaction template card.

    buttons: [{"text": str, "style": int, "key": str}]
    """
    card: dict = {
        "card_type": "button_interaction",
        "source": {"desc": source or "SuperCC"},
        "main_title": {"title": title, "desc": description},
    }
    if buttons:
        card["button_list"] = buttons
    return card
