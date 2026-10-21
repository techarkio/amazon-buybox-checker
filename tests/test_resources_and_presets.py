import pytest
from fastapi.testclient import TestClient

from buybox_checker import resources
from buybox_checker.jobs import JobManager
from buybox_checker.resources import parse_sessions, recommend
from buybox_checker.states import GMV_TIERS, PRESETS, STATES, pick_states
from buybox_checker.web.app import create_app

from conftest import fake_run_checks


def test_recommendation_takes_the_tightest_of_cpu_memory_and_ip():
    assert recommend(8, 16_000, True, 4).recommended_sessions == 4
    assert recommend(8, 16_000, True, 4).limited_by == "ip"
    small_box = recommend(1, 1_800, True, 4)  # 1 vCPU, 1.8 GB free: memory allows 2, CPU allows 2
    assert small_box.recommended_sessions == 2 and small_box.limited_by != "ip"
    assert recommend(4, 1_200, True, 4).limited_by == "memory"
    assert recommend(0.5, 800, True, 4).recommended_sessions == 1  # never below one
    assert recommend(1.5, None, False, 4).limited_by == "cpu"  # memory unknown (Windows): CPU decides


def test_container_limits_lower_the_cores_and_free_memory(tmp_path, monkeypatch):
    (tmp_path / "cpu.max").write_text("150000 100000\n")
    (tmp_path / "memory.max").write_text(str(3 * 2**30))
    (tmp_path / "memory.current").write_text(str(1 * 2**30))
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 65000000 kB\nMemAvailable: 60000000 kB\n")
    monkeypatch.setattr(resources, "CGROUP_DIR", tmp_path)
    monkeypatch.setattr(resources, "MEMINFO", meminfo)
    assert resources.cpu_cores() <= 1.5
    assert resources.free_memory_mb() == 2048
    assert resources.detect_resources(6).recommended_sessions == 3  # (2048 - 512) / 512


def test_sessions_option_accepts_a_number_or_auto():
    assert parse_sessions("auto") is None and parse_sessions(" AUTO ") is None
    assert parse_sessions("3") == 3
    with pytest.raises(ValueError):
        parse_sessions("many")


def test_presets_are_built_from_the_market_tiers():
    assert {key: len(preset.codes) for key, preset in PRESETS.items()} == {"core": 4, "core-growth": 12}
    assert [tier.name for tier in GMV_TIERS] == ["Core markets", "Growth markets"]
    assert all(code in STATES for tier in GMV_TIERS for code in tier.codes)
    assert list(pick_states(["core"])) == ["CA", "TX", "FL", "NY"]
    assert len(pick_states(["CORE-GROWTH", "ak", "CA"])) == 13  # presets mix with codes, duplicates dropped


def test_web_config_offers_tiers_presets_and_the_detected_sessions():
    config = TestClient(create_app(manager=JobManager(run_checks=fake_run_checks))).get("/api/config").json()
    assert [tier["key"] for tier in config["tiers"]] == ["core", "growth"]
    assert [(p["key"], p["name"]) for p in config["presets"]] == [("core", "Core markets"),
                                                                  ("core-growth", "Core + growth")]
    assert config["default_sessions"] == resources.detect_resources(6).recommended_sessions

    pinned = TestClient(create_app(manager=JobManager(run_checks=fake_run_checks), default_sessions=1))
    assert pinned.get("/api/config").json()["default_sessions"] == 1


def test_web_job_without_sessions_uses_the_recommendation():
    client = TestClient(create_app(manager=JobManager(run_checks=fake_run_checks)))
    response = client.post("/api/jobs", headers={"X-Buybox-Checker": "1"},
                           data={"text": "B0BCMLGPYB", "states": "core"})
    assert response.status_code == 200, response.text
    assert response.json()["job"]["states"] == ["CA", "TX", "FL", "NY"]
