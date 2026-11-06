"""监控配置升级时的 Grafana 私网迁移契约。"""

from app.db.bootstrap import migrate_managed_grafana_url


def test_generated_public_grafana_url_migrates_to_internal_network():
    config = {"grafana_url": "http://192.168.1.10:3000"}

    changed = migrate_managed_grafana_url(
        config,
        monitoring_host="192.168.1.10",
        target_url="http://grafana:3000",
    )

    assert changed is True
    assert config["grafana_url"] == "http://grafana:3000"


def test_manually_configured_grafana_url_is_never_overwritten():
    config = {"grafana_url": "https://grafana.example.com"}

    changed = migrate_managed_grafana_url(
        config,
        monitoring_host="192.168.1.10",
        target_url="http://grafana:3000",
    )

    assert changed is False
    assert config["grafana_url"] == "https://grafana.example.com"
