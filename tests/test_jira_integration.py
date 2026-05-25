from agentic.integrations.jira import _adf_to_text


def test_adf_to_text_extracts_description_specs():
    adf = {
        "type": "doc",
        "content": [
            {
                "type": "heading",
                "content": [{"type": "text", "text": "Specs"}],
            },
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Driver lookup by user ID."}],
            },
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "TypeCD must be driver"}],
                            }
                        ],
                    }
                ],
            },
        ],
    }

    out = _adf_to_text(adf)

    assert "Specs" in out
    assert "Driver lookup by user ID." in out
    assert "- TypeCD must be driver" in out
