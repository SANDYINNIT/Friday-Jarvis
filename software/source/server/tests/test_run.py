# test_main.py
#
# INHERITED UPSTREAM SCAFFOLDING - NOT FRIDAY CODE.
#
# This shipped with Open Interpreter and shells out to `poetry run 01`, then
# waits for the literal word "Hold" in the output. FRIDAY does not use Poetry
# (it runs from a venv / plain `python` against its own main.py) and her output
# never contains "Hold", so this test can never pass here - it would block for
# 30s and then assert False on every single run. The sibling test below was
# already disabled upstream for the same reason ("pytest hanging").
#
# Kept for reference, skipped explicitly so the suite stays green and the
# reason is visible instead of mysterious.
import pytest


import subprocess
import time

@pytest.mark.skip(reason=(
    "upstream Open Interpreter scaffold: requires `poetry run 01` and greps for "
    "'Hold'. FRIDAY runs from a venv, not Poetry, so this can never pass."
))
def test_poetry_run_01():
    process = subprocess.Popen(['poetry', 'run', '01'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    timeout = time.time() + 30  # 30 seconds from now

    while True:
        output = process.stdout.readline().decode('utf-8')
        if "Hold" in output:
            assert True
            return
        if time.time() > timeout:
            assert False, "Timeout reached without finding expected output."
            return

# @pytest.mark.skip(reason="pytest hanging")
# def test_ping(client):
#     response = client.get("/ping")
#     assert response.status_code == 200
#     assert response.text == "pong"


# def test_interpreter_chat(mock_interpreter):
#     # Set up a sample conversation
#     messages = [
#         {"role": "user", "type": "message", "content": "Hello."},
#         {"role": "assistant", "type": "message", "content": "Hi there!"},
#         # Add more messages as needed
#     ]

#     # Configure the mock interpreter with the sample conversation
#     mock_interpreter.messages = messages

#     # Simulate additional user input
#     user_input = {"role": "user", "type": "message", "content": "How are you?"}
#     mock_interpreter.chat([user_input])

#     # Ensure the interpreter processed the user input
#     assert len(mock_interpreter.messages) == len(messages)
#     assert mock_interpreter.messages[-1]["role"] == "assistant"
#     assert "don't have feelings" in mock_interpreter.messages[-1]["content"]

# def test_interpreter_configuration(mock_interpreter):
#     # Test interpreter configuration
#     interpreter = configure_interpreter(mock_interpreter)
#     assert interpreter is not None
