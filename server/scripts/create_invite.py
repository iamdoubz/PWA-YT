"""Mint an invite code.

    uv run python scripts/create_invite.py [created_by_user_id]

Registration is invite-only (D-008) and there is deliberately no endpoint for
it — inviting someone is an operator action, not something the app exposes to
users. Run with no argument to mint the very first code (bootstrap, before any
user exists); pass an existing user's id to attribute the invite to them.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402


def main() -> None:
    db.init()
    created_by = sys.argv[1] if len(sys.argv) > 1 else None
    code = db.uuid7()[-12:]  # short enough to type or paste, still unguessable
    with db.writing() as conn:
        conn.execute(
            "INSERT INTO invites (code, created_by, created_at) VALUES (?, ?, ?)",
            (code, created_by, db.now()),
        )
    print(code)


if __name__ == "__main__":
    main()
