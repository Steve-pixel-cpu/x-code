def safe_name(name):
    return "".join(c for c in name if c.isalnum() or c in "-_.")


def split_lines(text):
    return [line for line in text.splitlines() if line.strip()]
