def capitalize_words(text):
    return " ".join(w.capitalize() for w in text.split())


def truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit - 1] + "…"


def is_blank(text):
    return not text or not text.strip()
