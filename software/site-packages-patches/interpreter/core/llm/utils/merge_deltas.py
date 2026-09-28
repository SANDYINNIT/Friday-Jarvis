def merge_deltas(original, delta):
    """
    Pushes the delta into the original and returns that.

    Great for reconstructing OpenAI streaming responses -> complete message objects.
    """

    for key, value in dict(delta).items():
        if value != None:
            if isinstance(value, str):
                if key in original:
                    original[key] = (original[key] or "") + (value or "")
                else:
                    original[key] = value
            else:
                # FRIDAY patch (2026-09-23): OpenRouter/litellm free-tier
                # streams occasionally emit a delta whose value is a LIST of
                # 4-element tuples (tool_calls) or another undict-able object.
                # `value = dict(value)` then crashed with "dictionary update
                # sequence element #0 has length 4; 2 is required", killing the
                # turn AND leaving the pipeline wedged (watchdog resets). Non-dict
                # deltas now replace cleanly instead of exploding.
                if isinstance(value, dict):
                    merged = value
                else:
                    try:
                        merged = dict(value)
                    except (TypeError, ValueError):
                        merged = value
                if key not in original:
                    original[key] = merged
                elif isinstance(merged, dict) and isinstance(original[key], dict):
                    merge_deltas(original[key], merged)
                else:
                    original[key] = merged

    return original