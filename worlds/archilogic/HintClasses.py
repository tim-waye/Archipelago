import logging
from enum import StrEnum
from typing import Dict, Tuple, Set

from BaseClasses import Item, Region, Location
from worlds.archilogic.errors import ArchiLogicError
from worlds.archilogic.games import GameProfile, profile_for, DEFAULT

_player_profiles: Dict[int, GameProfile] = {}

def set_player_profiles(games: Dict[int, str]) -> None:
    _player_profiles.clear()
    _player_profiles.update({player: profile_for(game) for player, game in games.items()})

# If any game has set display names in their profile use those.
# If they don't have a profile we default to hint_text, then name.
def get_display_name(obj: Item | Location | Region) -> str:
    profile = _player_profiles.get(obj.player, DEFAULT)
    return profile.display_name(obj) or obj.hint_text or obj.name

class Directness(StrEnum):
    Item = "item"
    Location = "location"
    Region = "region"

class HintType(StrEnum):
    GenericWoth = "generic_woth"
    SpecificWoth = "specific_woth"
    SinglePath = "single_path"
    MultiPath = "multi_path"

def flatten_item(item: Item) -> dict:
    return {"name": get_display_name(item), "id": item.code, "player": item.player}

class Hint:
    hint_type: HintType

    def __init__(self, hint_item: Item, directness: Directness):
        self.item = hint_item
        self.item_id = hint_item.code
        self.item_player = hint_item.player
        self.location_id = hint_item.location.address if hint_item.location else None
        self.location_player = hint_item.location.player if hint_item.location else None
        self.finder_id = hint_item.player if hint_item.location is None else hint_item.location.player
        # First half of hint text.
        self.subject_text, self.directness = write_item_hint_text(hint_item, directness)
        self.hint_text = ""

    def to_slot_data(self) -> dict:
        return {
            "hint_type": self.hint_type,
            "hint_text": self.hint_text,
            "item_id": self.item_id,
            "item_player": self.item_player,
            "location_id": self.location_id,
            "location_player": self.location_player,
            "directness": self.directness.value,
            "finder_id": self.finder_id,
        }

class WothHint(Hint):
    def __init__(
            self,
            hint_item: Item,
            directness: Directness,
            woth_players: Set[int],
            woth_player_names: Set[str],
            all_players: bool,
    ):
        super().__init__(hint_item, directness)
        self.woth_player_ids = woth_players

        # Write hint text. First half of the string is already initialized in the super constructor
        end_str = "is on the Path to "
        if all_players:
            end_str += "Everybody's "
        end_str += f"Victory"

        if not all_players and len(woth_players) > 0:
            self.hint_type = HintType.SpecificWoth
            end_str += f" for {concat_list(sorted(woth_player_names), ',', True)}"
        else:
            self.hint_type = HintType.GenericWoth

        self.hint_text = f"{self.subject_text}{end_str}."

    def to_slot_data(self) -> dict:
        slot_data = super().to_slot_data()
        slot_data["woth_player_ids"] = self.woth_player_ids
        return slot_data

class SinglePathHint(Hint):
    def __init__(
            self,
            hint_item: Item,
            endpoint: Item,
            directness: Directness,
    ):
        super().__init__(hint_item, directness)
        self.endpoint = endpoint
        self.hint_type = HintType.SinglePath

        # Write hint text. First half of the string is already initialized in the super constructor
        self.hint_text = f"{self.subject_text}is on the Path to {get_display_name(endpoint)}."

    def to_slot_data(self) -> dict:
        slot_data = super().to_slot_data()
        slot_data["endpoint"] = flatten_item(self.endpoint)
        return slot_data

class MultiPathHint(Hint):
    def __init__(
            self,
            hint_item: Item,
            endpoints: Dict[Tuple[int, str], Set[Item]],
            directness: Directness,
    ):
        super().__init__(hint_item, directness)
        self.endpoints = endpoints
        self.hint_type = HintType.MultiPath

        # Write hint text. First half of the string is already initialized in the super constructor
        end_list = []
        for (_, player), ends in endpoints.items():
            end_names = sorted(get_display_name(end) for end in ends)
            end_list.append(f"{concat_list(end_names, ',', True)} for {player}")

        end_str = f"is on the Path to {concat_list(sorted(end_list), ';', True)}"
        self.hint_text = f"{self.subject_text}{end_str}."

    def to_slot_data(self) -> dict:
        slot_data = super().to_slot_data()
        slot_data["endpoints"] = [
            flatten_item(e)
            for _, ends in sorted(self.endpoints.items())
            for e in sorted(ends, key=get_display_name)
        ]
        return slot_data

def write_item_hint_text(item, directness) -> Tuple[str, Directness]:
    direct = directness
    if not item.location and directness == Directness.Region:
        logging.debug("Trying to construct a Region path hint for an item with a null parent region. "
                      "Downgrading to Directness.Location")
        direct = Directness.Location
    if not item.location and (directness == Directness.Region or directness == Directness.Location):
        logging.debug("Trying to construct a Location path hint for an item with a null location. "
                      "Downgrading to Directness.Item")
        direct = Directness.Item
    match direct:
        case Directness.Item:
            return f"{get_display_name(item)} ", direct
        case Directness.Location:
            if item.location:
                return f"Something on {get_display_name(item.location)} ", direct
            else:
                raise ArchiLogicError("Tried to make a Location path hint for an item with no location")
        case Directness.Region:
            if item.location and item.location.parent_region:
                return f"Something in {get_display_name(item.location.parent_region)} ", direct
            else:
                raise ArchiLogicError("Tried to make a Region path hint for an item with no parent region")
    raise ArchiLogicError(f"Unable to write hint text for item: {get_display_name(item)}")

def concat_list(parts: list[str], delimiter: str, oxford: bool) -> str:
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]}{delimiter} and {parts[1]}"
    return f"{delimiter} ".join(parts[:-1]) + f"{delimiter if oxford else ''} and {parts[-1]}"

