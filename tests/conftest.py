import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LIBS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     ".pylibs", "lib",
                     f"python{sys.version_info.major}.{sys.version_info.minor}",
                     "site-packages")
if os.path.isdir(_LIBS):
    sys.path.insert(0, _LIBS)

from app import create_app  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    app = create_app(database=str(tmp_path / "test.db"))
    app.config.update(TESTING=True)
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def make_cells(n, base_cap=100.0, spread=0.0, resist=2.0, ocv=3.2, temp=25.0):
    cells = []
    for i in range(n):
        cells.append({
            "cell_id": f"C{i + 1:03d}",
            "capacity_ah": round(base_cap + (spread * i - spread * (n - 1) / 2), 3),
            "resistance_mohm": round(resist + (i % 3) * 0.02, 3),
            "ocv_v": ocv,
            "cycles": 500 + i,
            "temperature_c": temp,
        })
    return cells


GOOD_BODY = {
    "name": "A批-8S1P",
    "rated_capacity_ah": 105.0,
    "topology": "8S1P",
    "thresholds": {
        "capacity_cv_max": 0.05,
        "resistance_cv_max": 0.10,
        "temperature_delta_max": 5.0,
        "ocv_delta_max": 0.05,
        "soh_min": 0.80,
    },
    "cells": make_cells(16, base_cap=100.0, spread=0.02),
}
