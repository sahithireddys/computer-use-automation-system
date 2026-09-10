"""
In-memory "core banking" data for the mock back-office app.

This deliberately mimics a small credit-union servicing system: members,
accounts, and a couple of permission-locked records to exercise the
"permission denied" and "not found" business outcomes the assignment
calls out.
"""
from dataclasses import dataclass, field
from typing import Optional
import itertools
import threading

_lock = threading.Lock()
_sub_account_seq = itertools.count(1000)


@dataclass
class Account:
    account_type: str
    balance_cents: int
    account_id: str


@dataclass
class Member:
    member_id: str
    first_name: str
    last_name: str
    status: str  # "active" | "restricted"
    accounts: list = field(default_factory=list)


MEMBERS = {
    "12345": Member(
        member_id="12345",
        first_name="Jordan",
        last_name="Alvarez",
        status="active",
        accounts=[Account("Savings", 184230, "SAV-12345-01")],
    ),
    "12346": Member(
        member_id="12346",
        first_name="Priya",
        last_name="Natarajan",
        status="active",
        accounts=[
            Account("Savings", 522, "SAV-12346-01"),
            Account("Checking", 10450, "CHK-12346-01"),
        ],
    ),
    "55555": Member(
        member_id="55555",
        first_name="Casey",
        last_name="Nguyen",
        status="active",
        accounts=[Account("Savings", 973050, "SAV-55555-01")],
    ),
    # Restricted record -> used to exercise the "permission denied" outcome.
    "99999": Member(
        member_id="99999",
        first_name="Restricted",
        last_name="Account",
        status="restricted",
        accounts=[Account("Savings", 0, "SAV-99999-01")],
    ),
}


def find_member(member_id: str) -> Optional[Member]:
    return MEMBERS.get(member_id.strip())


def open_sub_account(member_id: str, account_type: str, initial_deposit_cents: int) -> Account:
    with _lock:
        member = MEMBERS.get(member_id)
        if member is None:
            raise KeyError(member_id)
        new_id = f"{account_type[:3].upper()}-{member_id}-{next(_sub_account_seq)}"
        acct = Account(account_type=account_type, balance_cents=initial_deposit_cents, account_id=new_id)
        member.accounts.append(acct)
        return acct
