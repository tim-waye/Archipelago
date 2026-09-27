import logging
from enum import StrEnum
from typing import Dict, Tuple, Set

from BaseClasses import Item

class Directness(StrEnum):
    Item = "item"
    Location = "location"
    Region = "region"

class WothHint:
    def __init__(
            self,
            hint_item: Item,
            finder_name: str,
            directness: Directness,
            woth_players: Set[int],
            woth_player_names: Set[str],
            all_players: bool,
    ):
        self.item = hint_item
        self.item_id = hint_item.code
        if hint_item.location:
            self.location_id = hint_item.location.address
        self.finder_id = hint_item.player
        self.finder_name = finder_name
        self.woth_player_ids = woth_players
        self.woth_player_names = woth_player_names

        # Write hint text. Start with the half referencing the item
        try:
            hint_str, self.directness = write_item_hint_text(self.item, directness)
        except ArchiLogicError:
            raise ArchiLogicError(f"Error writing hint text for item: {self.item}")

        end_str = "is on the Path to "
        if all_players:
            end_str += " Everybody's "
        end_str += f"Victory"

        if not all_players and len(woth_players) > 0:
            end_str += f" for {concat_list(sorted(self.woth_player_names), ',', True)}"
        self.hint_text = f"{hint_str}{end_str}."

class SinglePathHint:
    def __init__(
            self,
            hint_item: Item,
            finder_name: str,
            endpoint: Item,
            directness: Directness,
    ):
        self.item = hint_item
        self.item_id = hint_item.code
        if hint_item.location:
            self.location_id = hint_item.location.address
        self.finder_id = hint_item.player
        self.finder_name = finder_name
        self.endpoint = endpoint

        # Write hint text. Start with the half referencing the item
        try:
            hint_str, self.directness = write_item_hint_text(self.item, directness)
        except ArchiLogicError:
            raise ArchiLogicError(f"Error writing hint text for item: {self.item}")

        # Second half gives the path
        self.hint_text = f"{hint_str} is on the path to {endpoint.name}"

class MultiPathHint:
    def __init__(
            self,
            hint_item: Item,
            finder_name: str,
            endpoints: Dict[Tuple[int, str], Set[Item]],
            directness: Directness,
    ):
        self.item = hint_item
        self.item_id = hint_item.code
        if hint_item.location:
            self.location_id = hint_item.location.address
        self.finder_id = hint_item.player
        self.finder_name = finder_name
        self.endpoints = endpoints

        # Write hint text. Start with the half referencing the item
        try:
            hint_str, self.directness = write_item_hint_text(self.item, directness)
        except ArchiLogicError:
            raise ArchiLogicError(f"Error writing hint text for item: {self.item}")

        # Second half lists all endpoints the item is on the path to.
        end_list = []
        for (_, player), ends in endpoints.items():
            end_list.append(f"{concat_list([end.name for end in ends], ",", True)} for {player}")

        end_str = f"is on the path to {concat_list(sorted(end_list), ';', True)}"
        self.hint_text = f"{hint_str}{end_str}."

class ArchiLogicError(RuntimeError):
    pass

def write_item_hint_text(item, directness) -> Tuple[str, Directness]:
    direct = directness
    if not item.location and directness == Directness.Region:
        logging.debug("Trying to construct a Region path hint for an item with a null parent region."
                      "Downgrading to Directness.Location")
        direct = Directness.Location
    if not item.location and (directness == Directness.Region or directness == Directness.Location):
        logging.debug("Trying to construct a Location path hint for an item with a null location."
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

def concat_list(parts: list[str], delimiter: str, oxford_delim: bool) -> str:
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]}{delimiter if oxford_delim else ""} and {parts[1]}"
    return f"{delimiter} ".join(parts[:-1]) + f"{delimiter if oxford_delim else ""} and {parts[-1]}"


