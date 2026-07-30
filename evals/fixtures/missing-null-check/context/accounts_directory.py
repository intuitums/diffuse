"""Account ownership lookups."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class User:
    id: str
    email: str


class Directory:
    def __init__(self, owners: dict[str, User]) -> None:
        self._owners = owners

    def lookup_owner(self, account_id: str) -> User | None:
        """The account's owner, or None.

        Accounts created by an automation token have no owner, and accounts
        keep existing after their owner is deleted, so None is a routine
        result rather than an error case.
        """
        return self._owners.get(account_id)

    def deliver(self, address: str, body: str) -> None:
        raise NotImplementedError
