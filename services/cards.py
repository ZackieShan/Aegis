"""A card's content as plain markdown, for a screen that does not draw cards.

The family screen draws an answer's card (static/lite/home.js). The full app does not: there a job that
answers with a short line and a card — an outing's plan, a recipe found for dinner — would show the line
alone. ``as_markdown`` writes what the card holds under that line instead, so nothing the job found is
missing on either screen. Cards whose answer already says everything in words (a forecast, a
conversion, a list) return "".
"""

from __future__ import annotations

from typing import Any, Dict, List


def _link(label: str, url: str) -> str:
    label = str(label or "").replace("[", "(").replace("]", ")").strip() or "Open"
    return f"[{label}]({url})" if url else label


def as_markdown(card: Dict[str, Any]) -> str:
    """What a trip card or a recipe card holds, as markdown; "" for every other card."""
    if not isinstance(card, dict):
        return ""
    lines: List[str] = []
    if card.get("type") == "trip":
        lines.append(f"**{card.get('title', 'The plan')}** · {' · '.join(str(part) for part in (card.get('when'), card.get('place')) if part)}")
        if card.get("note"):
            lines.append(f"_{card['note']}_")
        times = card.get("times") or []
        if times:
            lines.append(" · ".join(f"{tile.get('label')} **{tile.get('value')}**" for tile in times))
        if card.get("assumed"):
            lines.append(str(card["assumed"]))
        weather = card.get("weather")
        if weather:
            said = [str(weather.get("summary") or "")] + [f"{str(tile.get('label', '')).lower()} {tile.get('value')}" for tile in weather.get("tiles") or []]
            lines.append("Weather: " + ", ".join(part for part in said if part) + ".")
            for alert in weather.get("alerts") or []:
                lines.append(f"**{alert.get('event')}** {alert.get('until') or ''}. {alert.get('text') or ''}".strip())
        elif card.get("no_forecast"):
            lines.append(str(card["no_forecast"]))
        watch = card.get("watch_for") or []
        if watch:
            lines.append("\n**Watch for**")
            for item in watch:
                source = item.get("source") or {}
                lines.append(f"- **{item.get('name')}** ({item.get('why')}): {item.get('text') or ''}"
                             + (f" — {_link(source.get('label') or 'the page', source.get('url', ''))}" if source.get("url") else ""))
        packed = card.get("list")
        if packed:
            items = packed.get("items") or []
            more = int(packed.get("open") or 0) - len(items)
            lines.append("\n**What to pack**" + (" (the usual essentials)" if card.get("essentials") else "") + ": "
                         + ", ".join(str(item) for item in items) + (f", and {more} more" if more > 0 else "") + ". "
                         + _link("Open the packing list", packed.get("href", "")))
        if card.get("reminder"):
            lines.append(f"Reminder set for {card['reminder']}.")
    elif card.get("type") == "recipe" and (card.get("from") or card.get("needs") or card.get("start")):
        about = " · ".join(str(part) for part in (card.get("serves"), card.get("time")) if part)
        lines.append(f"**{card.get('title', 'The recipe')}**" + (f" · {about}" if about else ""))
        if card.get("needs"):
            lines.append("It also needs: " + ", ".join(str(item) for item in card["needs"]) + ".")
        if card.get("added"):
            lines.append(f"On your {card['added']}: " + ", ".join(str(item) for item in card.get("added_items") or []) + ".")
        doors = [_link("Start cooking", card.get("href", ""))]
        if card.get("source_url"):
            doors.append(_link("The page it is from", card["source_url"]))
        lines.append(" · ".join(doors))
    return "\n\n".join(line for line in lines if line).replace("\n\n\n", "\n\n")
