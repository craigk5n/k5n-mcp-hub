def extract_sse_data(body: bytes | str) -> bytes | None:
    if isinstance(body, bytes):
        try:
            body_str = body.decode("utf-8")
        except UnicodeDecodeError:
            return None
    else:
        body_str = body

    if "data:" not in body_str:
        return None

    data_parts: list[str] = []
    found_data = False

    for line in body_str.split("\n"):
        if line.startswith("data:"):
            found_data = True
            payload = line[5:]
            if payload.strip() == "[DONE]":
                return None
            data_parts.append(payload)
        elif found_data and line.strip() == "":
            break
        elif found_data and not line.startswith("data:"):
            break

    if not data_parts:
        return None

    concatenated = "\n".join(data_parts)
    result = concatenated.strip()

    if result == "":
        return b""

    return result.encode("utf-8")
