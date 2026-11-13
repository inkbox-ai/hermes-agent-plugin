"""Compatibility names delegated to the current remote catalog."""

COMPATIBILITY_NAMES = {
    "inkbox_lookup_contact": "inkbox_contacts_list",
    "inkbox_list_contacts": "inkbox_contacts_list",
    "inkbox_get_contact": "inkbox_contact_get",
    "inkbox_create_contact": "inkbox_contact_create",
    "inkbox_update_contact": "inkbox_contact_update",
    "inkbox_delete_contact": "inkbox_contact_delete",
}


def compatibility_arguments(name, arguments, operation_id=None):
    from .tools import _contact_arg, _contact_payload

    if name not in COMPATIBILITY_NAMES:
        return arguments
    if name == "inkbox_lookup_contact":
        fields = {
            key: value
            for key, camel in (
                ("email", "email"),
                ("phone", "phone"),
                ("email_domain", "emailDomain"),
                ("email_contains", "emailContains"),
                ("phone_contains", "phoneContains"),
            )
            if (value := _contact_arg(arguments, key, camel))
        }
        if len(fields) != 1:
            raise ValueError("Contact lookup requires exactly one lookup criterion")
        return fields
    if name == "inkbox_list_contacts":
        return {"limit": 25, **arguments}
    if name in {"inkbox_get_contact", "inkbox_delete_contact"}:
        result = {"contact_id": _contact_arg(arguments, "contact_id", "contactId")}
        return {**result, "idempotency_key": operation_id} if name == "inkbox_delete_contact" else result
    fields = _contact_payload(arguments, require_any=True)
    for key, value_key in (("emails", "value"), ("phones", "value_e164")):
        if key in fields:
            fields[key] = [
                {value_key: value.value, "label": value.label, "is_primary": value.is_primary} for value in fields[key]
            ]
    result = (
        {"contact": fields}
        if name == "inkbox_create_contact"
        else {"contact_id": _contact_arg(arguments, "contact_id", "contactId"), "patch": fields}
    )
    return {**result, "idempotency_key": operation_id}
