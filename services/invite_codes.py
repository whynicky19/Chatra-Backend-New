import secrets

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6


def legacy_deterministic_code(class_id: int) -> str:
    """Reproduces the old client-side (Flutter/Nuxt) invite code formula bit-for-bit,
    so codes already printed/sent for existing classes keep working after backfill."""
    n = class_id * 1337 + 42
    code = ""
    for _ in range(CODE_LENGTH):
        code += ALPHABET[n % 32]
        n = n // 32 + class_id * 7
    return code


def random_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def generate_unique_code(db, max_attempts: int = 20) -> str:
    from models import Class

    for _ in range(max_attempts):
        code = random_code()
        exists = db.query(Class).filter(Class.invite_code == code).first()
        if not exists:
            return code
    raise RuntimeError("Could not generate a unique invite code after several attempts")