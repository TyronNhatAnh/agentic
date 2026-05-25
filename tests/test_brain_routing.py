from agentic.brain import parse_decision


def test_parse_direct_reply():
    raw = '{"reply": "hello", "need_clarification": false, "steps": [], "actions": []}'
    d = parse_decision(raw)
    assert d.reply == "hello"
    assert d.steps == []
    assert not d.need_clarification


def test_parse_clarification():
    raw = '{"need_clarification": true, "clarify_question": "Which repo?", "steps": []}'
    d = parse_decision(raw)
    assert d.need_clarification
    assert d.clarify_question == "Which repo?"


def test_parse_steps_and_actions():
    raw = """```json
    {
      "reply": null,
      "need_clarification": false,
      "steps": [
        {"agent": "ba", "task": "write story for login"},
        {"agent": "dev", "task": "draft code"}
      ],
      "actions": [
        {"type": "github.create_issue", "payload": {"title": "x", "body": "y"}}
      ]
    }
    ```"""
    d = parse_decision(raw)
    assert [s.agent for s in d.steps] == ["ba", "dev"]
    assert d.actions[0].type == "github.create_issue"
    assert d.actions[0].payload["title"] == "x"
