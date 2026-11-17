"""Ask Hopwise one question from the terminal."""

import argparse

from agent import AgentError, answer_question_from_env


def main():
    parser = argparse.ArgumentParser(description="Ask Hopwise about Melbourne trains.")
    parser.add_argument("question", nargs="?",
                        default="What are the next trains from Richmond, and are there any disruptions?")
    args = parser.parse_args()
    try:
        answer = answer_question_from_env(args.question)
    except (AgentError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    print(answer)


if __name__ == "__main__":
    main()
