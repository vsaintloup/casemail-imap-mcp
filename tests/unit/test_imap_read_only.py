from __future__ import annotations

import types

from casemail_imap_mcp.imap_client import ReadOnlyImapClient


class FakeImap:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.uidvalidity = [b"12345"]

    def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append(("login", (username, password)))
        return "OK", [b"logged in"]

    def logout(self) -> tuple[str, list[bytes]]:
        self.calls.append(("logout", ()))
        return "BYE", [b"logged out"]

    def list(self) -> tuple[str, list[bytes]]:
        self.calls.append(("list", ()))
        return "OK", [b'(\\HasNoChildren) "/" "Client/ABC"']

    def status(self, folder: str, items: str) -> tuple[str, list[bytes]]:
        self.calls.append(("status", (folder, items)))
        return "OK", [b'"Client/ABC" (MESSAGES 2 UIDVALIDITY 12345)']

    def examine(self, folder: str) -> tuple[str, list[bytes]]:
        self.calls.append(("examine", (folder,)))
        return "OK", [b"2"]

    def response(self, code: str):
        self.calls.append(("response", (code,)))
        return code, self.uidvalidity

    def uid(self, command: str, *args):
        self.calls.append(("uid", (command, *args)))
        if command == "SEARCH":
            return "OK", [b"1 2"]
        if command == "FETCH":
            return "OK", [(b'1 (UID 2 INTERNALDATE "02-Feb-2026 10:00:00 +0000" BODY[] {12}', b"Hello world!"), b")"]
        raise AssertionError(f"unexpected UID command {command}")

    def noop(self):
        self.calls.append(("noop", ()))
        return "OK", [b""]


def test_read_only_client_uses_examine_and_body_peek(monkeypatch, settings) -> None:
    fake = FakeImap()
    monkeypatch.setattr("imaplib.IMAP4_SSL", lambda host, port: fake)

    with ReadOnlyImapClient(settings) as client:
        client.list_folders(include_counts=True)
        uidvalidity, uids = client.search_uids("Client/ABC")
        fetched = client.fetch_message("Client/ABC", uids[-1], uidvalidity)

    commands = [name for name, _ in fake.calls]
    uid_commands = [args for name, args in fake.calls if name == "uid"]

    assert "examine" in commands
    assert uidvalidity == 12345
    assert fetched.raw_bytes == b"Hello world!"
    assert any(command[0] == "FETCH" and "BODY.PEEK[]" in command[2] for command in uid_commands)
    assert "store" not in commands
    assert "copy" not in commands
    assert "append" not in commands

