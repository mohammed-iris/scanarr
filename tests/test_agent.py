from app import agent, stack, updates

COMPOSE = f"""services:
  init:
    image: {stack.IMAGE}:1.0.0
  qbittorrent:
    image: lscr.io/linuxserver/qbittorrent:5.3.1
  prowlarr:
    image: lscr.io/linuxserver/prowlarr:3.1.0
  scanarr:
    image: {stack.IMAGE}:1.0.0
  updater:
    image: {stack.IMAGE}:1.0.0
"""


def test_rewrite_moves_all_scanarr_images_and_pins():
    new = agent.rewrite(COMPOSE, "1.1.0", {"qbittorrent": "lscr.io/linuxserver/qbittorrent:5.4.0",
                                           "prowlarr": "lscr.io/linuxserver/prowlarr:3.2.0"})
    assert new.count(f"{stack.IMAGE}:1.1.0") == 3 and "1.0.0" not in new
    assert "qbittorrent:5.4.0" in new and "prowlarr:3.2.0" in new
    assert agent.current_versions(new)["scanarr"] == "1.1.0"


def test_rewrite_ignores_odd_pins():
    new = agent.rewrite(COMPOSE, "1.1.0", {"qbittorrent": "evil; rm -rf /"})
    assert "qbittorrent:5.3.1" in new


def test_version_compare():
    assert updates.newer("1.10.0", "1.9.9") and updates.newer("v2.0.0", "1.99.99")
    assert not updates.newer("1.0.0", "1.0.0") and not updates.newer("garbage", "1.0.0")
