from inkbox_plugin.remote_compat import compatibility_arguments


def test_native_contact_schema_preserves_camel_case_and_labeled_entries():
    assert compatibility_arguments("inkbox_lookup_contact", {"emailDomain": "example.com"}) == {
        "email_domain": "example.com"
    }
    assert compatibility_arguments("inkbox_get_contact", {"contactId": "contact"}) == {"contact_id": "contact"}
    result = compatibility_arguments(
        "inkbox_update_contact",
        {
            "contactId": "contact",
            "givenName": "Example",
            "phones": [{"value": "+15550000001", "label": "work", "isPrimary": False}],
            "emails": [],
        },
        "native-call",
    )
    assert result == {
        "contact_id": "contact",
        "patch": {
            "given_name": "Example",
            "phones": [{"value_e164": "+15550000001", "label": "work", "is_primary": False}],
            "emails": [],
        },
        "idempotency_key": "native-call",
    }
