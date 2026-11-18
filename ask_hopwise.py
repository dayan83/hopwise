"""Ask Hopwise one question from the terminal."""

import argparse
import json
import sys

from agent import AgentError, answer_question_from_env
from agent_trace import AgentTrace


def main():
    parser = argparse.ArgumentParser(description="Ask Hopwise about Melbourne trains.")
    parser.add_argument("question", nargs="?",
                        default="What are the next trains from Richmond, and are there any disruptions?")
    parser.add_argument("--trace", action="store_true",
                        help="Print a sanitized JSON execution trace to stderr after the answer.")
    args = parser.parse_args()
    trace = AgentTrace() if args.trace else None
    try:
        if trace is None:
            answer = answer_question_from_env(args.question)
        else:
            answer = answer_question_from_env(args.question, trace=trace)
    except (AgentError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    else:
        print(answer)
    finally:
        if trace is not None:
            print(json.dumps(trace.as_dict(), indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
