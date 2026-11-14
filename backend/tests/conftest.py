import pytest

from tradelab.config import Settings
from tradelab.storage.db import Database, Ledger
from tradelab.simulations import Simulations


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.sqlite"))
    yield d
    d.close()


@pytest.fixture
def ledger(db):
    return Ledger(db)


@pytest.fixture
def sims(db, ledger):
    return Simulations(db, ledger)


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, db_path=str(tmp_path / "api.sqlite"), status_push_seconds=0.05,
                    alpaca_api_key="k-secret-value", alpaca_api_secret="s-secret-value")
