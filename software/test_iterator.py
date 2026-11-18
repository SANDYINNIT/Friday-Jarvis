from litellm.llms.ollama.chat.transformation import OllamaChatCompletionResponseIterator

x = OllamaChatCompletionResponseIterator(
    streaming_response=iter([]),
    sync_stream=True,
)

r = x.chunk_parser({
    "model": "qwen3:8b",
    "message": {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "function": {
                "name": "execute",
                "arguments": {
                    "language": "python",
                    "code": 'print("Hello")'
                }
            }
        }]
    },
    "done": False,
})

print("CONTENT =", repr(r.choices[0].delta.content))
print("TOOL_CALLS =", repr(r.choices[0].delta.tool_calls))