"""Limits shared by data trust boundaries; no shop code or model imports."""

import json
import math
import re

MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_TOKENS = 100_000
MAX_MODEL_TOKENS = 90_000
MAX_DIAGNOSTICS = 100
MAX_EVALUATION_WORK = 100_000
MAX_RESULT_WEIGHT = 256_000
MAX_WORKFLOW_STATE = 512
MAX_CASE_WORK = 1_000_000

_STRUCTURAL = re.compile(r'["{}\[\],:]')
_STRING_SPECIAL = re.compile(r'["\\]')


def bounded_json(
    data: bytes, *, max_bytes: int = MAX_JSON_BYTES, max_tokens: int = MAX_JSON_TOKENS
) -> object:
    """Preflight allocation/depth, then use the standard JSON grammar decoder.

    The scan visits special characters once, skipping plain string contents in
    the regex engine. Tokens count structural punctuation, string openings, and
    escapes; escaped punctuation never changes nesting. Limits precede allocation.
    """
    if len(data) > min(max_bytes, MAX_JSON_BYTES):
        raise ValueError("JSON exceeds the byte size limit")
    source = data.decode("utf-8")
    position = depth = tokens = 0
    limit = min(max_tokens, MAX_JSON_TOKENS)
    while (match := _STRUCTURAL.search(source, position)) is not None:
        position, character = match.end(), match[0]
        tokens += 1
        if tokens > limit:
            raise ValueError("JSON exceeds the structural token limit")
        if character == '"':
            while True:
                closing = _STRING_SPECIAL.search(source, position)
                if closing is None:
                    raise ValueError("unterminated JSON string")
                position = closing.end()
                if closing[0] == '"':
                    break
                tokens += 1
                if tokens > limit:
                    raise ValueError("JSON exceeds the structural token limit")
                position += 1  # skip the escaped character, including a quote or backslash
        elif character in "{[":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError("JSON exceeds the nesting limit")
        elif character in "}]":
            depth -= 1
            if depth < 0:
                raise ValueError("invalid JSON nesting")

    def pairs(items: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    def number(value: str) -> int | float:
        if len(value) > 100:
            raise ValueError("JSON number exceeds the length limit")
        if "." not in value and "e" not in value.lower():
            return int(value)
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("JSON numbers must be finite")
        return result

    def constant(value: str) -> object:
        raise ValueError("JSON numbers must be finite")

    try:
        return json.loads(
            source,
            object_pairs_hook=pairs,
            parse_int=number,
            parse_float=number,
            parse_constant=constant,
        )
    except RecursionError as error:
        raise ValueError("JSON exceeds the nesting limit") from error
