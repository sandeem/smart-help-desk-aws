"""
Tests for the non-question (chit-chat) guard in query.py.

Run locally — no AWS credentials, no database needed:
    python project/test_guard.py

The guard is pure Python (regex + keywords), so it is fully testable offline.
That is the point: refusing small talk costs zero Bedrock calls and zero DB queries.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from query import is_chitchat  # noqa: E402

# Inputs that must be REFUSED (small talk / not a support question)
SHOULD_REFUSE = [
    "hi",
    "Hi!",
    "hello",
    "hey there",
    "yo",
    "good morning",
    "Good evening!",
    "thanks",
    "Thank you!",
    "thx",
    "bye",
    "goodbye",
    "see you later",
    "ok",
    "okay",
    "cool",
    "got it",
    "how are you?",
    "who are you",
    "are you a bot?",
    "are you human",
    "what's your name?",
    "lol",
    "hmmm",
    "test",
    "testing",
    "",
    "   ",
    "?",
    "a",
]

# Inputs that must PASS THROUGH to retrieval (genuine support questions)
SHOULD_ANSWER = [
    "How do I reset my password?",
    "how do i reset my password",
    "My order never arrived",
    "hi, how do I cancel my subscription?",
    "hey there, my account is locked",
    "thanks — but why was I charged twice?",
    "I need a refund",
    "The app keeps crashing on login",
    "what is your return policy",
    "Where is my delivery?",
    "Can I upgrade my plan?",
    "billing error on my last invoice",
    "How long does shipping take?",
    "contact support",
]


def main():
    failures = []

    for text in SHOULD_REFUSE:
        if not is_chitchat(text):
            failures.append(f"  Expected REFUSE but passed through: {text!r}")

    for text in SHOULD_ANSWER:
        if is_chitchat(text):
            failures.append(f"  Expected ANSWER but was refused: {text!r}")

    total = len(SHOULD_REFUSE) + len(SHOULD_ANSWER)
    passed = total - len(failures)

    print(f"Guard tests: {passed}/{total} passed")
    if failures:
        print("\nFAILURES:")
        print("\n".join(failures))
        sys.exit(1)
    print("All cases correct — chit-chat refused, support questions pass through.")


if __name__ == "__main__":
    main()
