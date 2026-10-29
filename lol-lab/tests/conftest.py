from copy import deepcopy
from pathlib import Path
import pytest


def blank_image():
    import cv2
    import numpy as np
    return cv2.imencode('.png', np.full((432, 768, 3), (55, 95, 55), dtype=np.uint8))[1].tobytes()


def raw_state(t=30, cs=0, deaths=0):
    def player(name, bot, team):
        return {"riotId": name, "summonerName": name, "rawChampionName": "game_character_displayname_Garen",
                "championName": "盖伦", "isBot": bot, "isDead": False, "team": team,
                "scores": {"creepScore": cs if not bot else 0, "kills": 0, "deaths": deaths if not bot else 0, "assists": 0}}
    return {"activePlayer": {"riotId": "private-account#test", "championStats": {"currentHealth": 600, "maxHealth": 600},
                            "currentGold": 500, "level": 1,
                            "abilities": {k: {"abilityLevel": 0} for k in "QWER"}},
            "allPlayers": [player("private-account#test", False, "ORDER"), player("Garen Bot", True, "CHAOS")],
            "gameData": {"gameTime": t, "gameMode": "CLASSIC", "mapName": "Map11"}, "events": {"Events": []}}


@pytest.fixture
def raw():
    return raw_state()


@pytest.fixture
def config(tmp_path):
    import json
    value = json.loads((Path(__file__).parents[1] / "examples/config.template.json").read_text())
    for key in ("game_executable", "ffmpeg_executable", "riot_ca_certificate"):
        path = tmp_path / key
        path.write_bytes(b"test fixture only")
        value[key] = str(path.resolve())
    value["call_budget"] = 20000
    return deepcopy(value)
