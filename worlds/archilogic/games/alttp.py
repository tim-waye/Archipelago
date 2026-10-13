from BaseClasses import MultiWorld
from ._profile import GameProfile, CustomEndpoint

class LinkToThePast(GameProfile):
    game = "A Link to the Past"

    prefer_loc_over_region = False

    prefer_hint_text = {
        "item": False,
        "location": False,
        "region": False,
    }

    def item_is_excluded(self, multiworld: MultiWorld, item_name: str) -> bool:
        return "Key" in item_name
