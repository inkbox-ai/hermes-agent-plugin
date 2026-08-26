#!/usr/bin/env python3

import os

from inkbox import Inkbox


def resolve_identity(client_factory=Inkbox) -> str:
    client = client_factory(
        api_key=os.environ["HERMES_INKBOX_API_KEY"],
        base_url=os.environ.get("INKBOX_BASE_URL", "https://inkbox.ai"),
    )
    return client.mailboxes.list()[0].email_address.split("@", 1)[0]


if __name__ == "__main__":
    print(resolve_identity())
