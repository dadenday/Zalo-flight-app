"""Shared helper functions for worker modules."""

import re


def format_time_to_minutes(time_str: str) -> int:
    """Parse time strings like 'in 25 min' or '1h 5m' into total minutes."""

    time_str = time_str.lower()
    total_minutes = 0
    try:
        if 'h' in time_str:
            parts = time_str.replace('in', '').strip().split('h')
            total_minutes += int(parts[0]) * 60
            if len(parts) > 1 and parts[1]:
                m_part = re.search(r'(\d+)', parts[1])
                if m_part:
                    total_minutes += int(m_part.group(1))
        elif 'm' in time_str:
            m_part = re.search(r'(\d+)', time_str)
            if m_part:
                total_minutes = int(m_part.group(1))
        elif ':' in time_str:
            parts = time_str.split(':')
            total_minutes = int(parts[0]) * 60 + int(parts[1])
        else:
            m_part = re.search(r'(\d+)', time_str)
            if m_part:
                total_minutes = int(m_part.group(1))
        return total_minutes
    except (ValueError, IndexError):
        return 0


def clean_text_for_summary(text: str) -> str:
    """Remove common boilerplate and unwanted text from downloaded files."""

    if not text:
        return ""
    patterns_to_remove = [
        r"Copyright [^\n]+",
        r"All rights reserved",
        r"Sign in to start your session",
        r"\s{2,}",  # Collapse whitespace
    ]
    for pat in patterns_to_remove:
        text = re.sub(pat, " ", text, flags=re.IGNORECASE)
    return text.strip()

