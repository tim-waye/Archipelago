import logging
from enum import StrEnum
from typing import Dict, Tuple, Set

from BaseClasses import Item

class Directness(StrEnum):
    Item = "item"
    Location = "location"
    Region = "region"

class HintType(StrEnum):
    GenericWoth = "generic_woth"
    SpecificWoth = "specific_woth"
    SinglePath = "single_path"
    MultiPath = "multi_path"

class ArchiLogicError(RuntimeError):
    pass

def flatten_item(item: Item) -> dict:
    return {"name": item.name, "id": item.code, "player": item.player}

class Hint:
    hint_type: HintType

    def __init__(self, hint_item: Item, finder_name: str, directness: Directness):
        self.item = hint_item
        self.item_id = hint_item.code
        self.item_player = hint_item.player
        self.location_id = hint_item.location.address if hint_item.location else None
        self.location_player = hint_item.location.player if hint_item.location else None
        self.finder_name = finder_name
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
            "finder_name": self.finder_name,
        }

class WothHint(Hint):
    def __init__(
            self,
            hint_item: Item,
            finder_name: str,
            directness: Directness,
            woth_players: Set[int],
            woth_player_names: Set[str],
            all_players: bool,
    ):
        super().__init__(hint_item, finder_name, directness)
        self.finder_id = hint_item.player
        self.woth_player_ids = woth_players
        self.woth_player_names = woth_player_names

        # Write hint text. First half of the string is already initialized in the super constructor
        end_str = "is on the Path to "
        if all_players:
            end_str += "Everybody's "
        end_str += f"Victory"

        if not all_players and len(woth_players) > 0:
            self.hint_type = HintType.SpecificWoth
            end_str += f" for {concat_list(sorted(self.woth_player_names), ',', True)}"
        else:
            self.hint_type = HintType.GenericWoth

        self.hint_text = f"{self.subject_text}{end_str}."

    def to_slot_data(self) -> dict:
        slot_data = super().to_slot_data()
        slot_data["finder_id"] = self.finder_id
        slot_data["woth_player_ids"] = self.woth_player_ids
        slot_data["woth_player_names"] = self.woth_player_names
        return slot_data

class SinglePathHint(Hint):
    def __init__(
            self,
            hint_item: Item,
            finder_name: str,
            endpoint: Item,
            directness: Directness,
    ):
        super().__init__(hint_item, finder_name, directness)
        self.finder_id = hint_item.player
        self.endpoint = endpoint
        self.hint_type = HintType.SinglePath

        # Write hint text. First half of the string is already initialized in the super constructor
        self.hint_text = f"{self.subject_text}is on the Path to {endpoint.name}."

    def to_slot_data(self) -> dict:
        slot_data = super().to_slot_data()
        slot_data["finder_id"] = self.finder_id
        slot_data["endpoint"] = flatten_item(self.endpoint)
        return slot_data

class MultiPathHint(Hint):
    def __init__(
            self,
            hint_item: Item,
            finder_name: str,
            endpoints: Dict[Tuple[int, str], Set[Item]],
            directness: Directness,
    ):
        super().__init__(hint_item, finder_name, directness)
        self.finder_id = hint_item.player
        self.endpoints = endpoints
        self.hint_type = HintType.MultiPath

        # Write hint text. First half of the string is already initialized in the super constructor
        end_list = []
        for (_, player), ends in endpoints.items():
            end_list.append(f"{concat_list([end.name for end in sorted(ends)], ',', True)} for {player}")

        end_str = f"is on the Path to {concat_list(sorted(end_list), ';', True)}"
        self.hint_text = f"{self.subject_text}{end_str}."

    def to_slot_data(self) -> dict:
        slot_data = super().to_slot_data()
        slot_data["finder_id"] = self.finder_id
        slot_data["endpoints"] = [
            flatten_item(e)
            for _, ends in sorted(self.endpoints.items())
            for e in sorted(ends, key=lambda x: x.name)
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
            return f"{item.hint_text} ", direct
        case Directness.Location:
            if item.location:
                return f"Something on {item.location.name} ", direct
            else:
                raise ArchiLogicError("Tried to make a Location path hint for an item with no location")
        case Directness.Region:
            if item.location and item.location.parent_region:
                return f"Something in {item.location.parent_region.hint_text} ", direct
            else:
                raise ArchiLogicError("Tried to make a Region path hint for an item with no parent region")
    raise ArchiLogicError(f"Unable to write hint text for item: {item.name}")

def concat_list(parts: list[str], delimiter: str, oxford: bool) -> str:
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]}{delimiter} and {parts[1]}"
    return f"{delimiter} ".join(parts[:-1]) + f"{delimiter if oxford else ''} and {parts[-1]}"

