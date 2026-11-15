"""Fail setup if native admission did not install the declared dependencies."""
from importlib.metadata import distribution
import json


def admitted_sdk_requirement():
    package = distribution("inkbox")
    source = package.read_text("direct_url.json")
    if source is None:
        return f"inkbox=={package.version}"
    source = json.loads(source)
    vcs = source.get("vcs_info", {})
    if vcs.get("vcs") != "git" or not vcs.get("commit_id"):
        raise RuntimeError("The admitted SDK source is not reproducible in the test runner")
    requirement = f"inkbox @ git+{source['url']}@{vcs['commit_id']}"
    if source.get("subdirectory"):
        requirement += f"#subdirectory={source['subdirectory']}"
    return requirement


def main():
    import aiohttp  # noqa: F401
    import audioop  # noqa: F401
    import inkbox  # noqa: F401
    import segno  # noqa: F401
    from gateway.status import read_runtime_status  # noqa: F401
    from inkbox.agent_identity import AgentIdentity

    assert callable(getattr(AgentIdentity, "send_whatsapp", None)), "Native runtime did not admit the real WhatsApp SDK candidate"
    assert callable(getattr(AgentIdentity, "get_whatsapp_media", None))
    print(admitted_sdk_requirement())


if __name__ == "__main__":
    main()
