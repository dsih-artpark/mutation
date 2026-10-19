"""deploy/compose.yaml must stay in step with the development docker-compose.yml."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEV = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
PROD = yaml.safe_load((ROOT / "deploy" / "compose.yaml").read_text())

APP_SERVICES = ["api", "worker", "beat"]
DEV_ONLY = {"dev", "minio"}


def test_production_runs_the_same_services_as_development_minus_the_dev_ones():
    assert set(PROD["services"]) == set(DEV["services"]) - DEV_ONLY


def test_production_builds_nothing():
    for name, service in PROD["services"].items():
        assert "build" not in service, f"{name} must run a published image, not build one"


@pytest.mark.parametrize("name", APP_SERVICES)
def test_app_services_use_the_published_image_with_a_required_tag(name):
    image = PROD["services"][name]["image"]
    # `:?` makes Compose refuse to start when APP_VERSION is unset.
    assert "${APP_VERSION:?" in image
    assert "latest" not in image


@pytest.mark.parametrize("name", APP_SERVICES)
def test_app_services_get_the_same_environment_and_mounts_as_in_development(name):
    dev, prod = DEV["services"][name], PROD["services"][name]
    assert prod["environment"] == dev["environment"]
    assert prod.get("volumes") == dev.get("volumes")
    assert prod.get("command") == dev.get("command")
    assert prod.get("group_add") == dev.get("group_add")
    assert prod["env_file"] == dev["env_file"]


@pytest.mark.parametrize("name", ["redis", "postgres"])
def test_backing_services_are_identical(name):
    assert PROD["services"][name] == DEV["services"][name]


def test_api_is_bound_to_loopback_unless_the_operator_opts_out():
    (port,) = PROD["services"]["api"]["ports"]
    assert port.startswith("${API_BIND:-127.0.0.1}:")


def test_project_name_is_pinned_so_volumes_do_not_follow_the_directory():
    assert PROD["name"].startswith("${COMPOSE_PROJECT_NAME:-")


def test_named_volumes_match_so_data_survives_a_switch_between_the_two_files():
    assert set(PROD["volumes"]) == set(DEV["volumes"])
