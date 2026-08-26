from considerate.identity import AgentIdentity, parse_header


def test_to_header_basic_fields():
    identity = AgentIdentity(name="MyBot", version="1.0", contact="mailto:a@b.com", intent="bulk-scrape")
    header = identity.to_header()
    parsed = parse_header(header)
    assert parsed["name"] == "MyBot"
    assert parsed["version"] == "1.0"
    assert parsed["contact"] == "mailto:a@b.com"
    assert parsed["intent"] == "bulk-scrape"


def test_to_header_omits_missing_contact():
    identity = AgentIdentity(name="MyBot")
    header = identity.to_header()
    assert "contact=" not in header


def test_to_header_sanitizes_quotes_and_delimiters():
    identity = AgentIdentity(name='Weird"Bot;Name')
    header = identity.to_header()
    parsed = parse_header(header)
    # Must not allow header/field injection via crafted names.
    assert '"' not in parsed["name"]
    assert ";" not in header.split("name=")[1].split(",")[0]


def test_extra_fields_included():
    identity = AgentIdentity(name="MyBot", extra={"rate": "1.0req/s"})
    parsed = parse_header(identity.to_header())
    assert parsed["rate"] == "1.0req/s"
