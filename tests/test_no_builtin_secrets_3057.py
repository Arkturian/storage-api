"""#3057: no secret may have a built-in fallback value.

After a rotation a missing variable must mean "no access", never the old,
publicly known value. Scans every Python file outside tests and venvs for
os.getenv/os.environ.get of a *KEY/*TOKEN/*SECRET/*PASSWORD variable with a
non-empty string default. Values are never printed, only file and name."""
import re
from pathlib import Path
from public_default_fixture import PUBLIC_DEFAULT_TEST  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PAT = re.compile(r'(?:os\.getenv|os\.environ\.get)\(\s*"([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS)[A-Z0-9_]*)"\s*,\s*"([^"]+)"')
SKIP = {"tests", "venv", ".venv", "node_modules", ".git", "__pycache__"}


def test_no_secret_has_a_builtin_default():
    hits = []
    for f in ROOT.rglob("*.py"):
        if SKIP & set(f.relative_to(ROOT).parts):
            continue
        for m in PAT.finditer(f.read_text(errors="ignore")):
            hits.append(f"{f.relative_to(ROOT)}: {m.group(1)}")
    assert not hits, "built-in secret defaults: " + ", ".join(hits)


def test_unset_master_authenticates_nobody(monkeypatch):
    """Without API_KEY in the environment neither the old literal nor an
    empty header gets in (checked against an empty database)."""
    import sys
    sys.path.insert(0, str(ROOT))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import auth
    import models
    models._hwm_ready.clear()
    eng = create_engine("sqlite://")
    models.Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    monkeypatch.setattr(auth.settings, "API_KEY", "")
    try:
        assert auth.verify_api_key(PUBLIC_DEFAULT_TEST, db) is None
        assert auth.verify_api_key("", db) is None
        assert db.query(models.User).filter(models.User.email == "system@api").count() == 0
    finally:
        db.close()
