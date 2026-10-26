from BaseClasses import Item, Location, Region
from ._profile import GameProfile
from ...hk import HKItem


class HollowKnight(GameProfile):
    game = "Hollow Knight"

    prefer_loc_over_region = True
