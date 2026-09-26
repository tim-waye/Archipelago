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
            length = len(woth_player_names)
            end_str += " for "
            for i, name in enumerate(self.woth_player_names):
                if i == length - 1:
                    end_str += "and "
                end_str += f"{name}"
                if i < length - 1:
                    end_str += ", "
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
        end_str = "is on the path to "
        for i, ((_, player), ends) in enumerate(endpoints.items()):
            for j, e in enumerate(ends):
                if j == 0:
                    if i == len(endpoints.keys()) - 1:
                        end_str += "and "
                    end_str += f"{player}'s"
                end_str += f" {e.name}"
                if j < len(ends) - 2:
                    end_str += ","
                if j == len(ends) - 2:
                    end_str += ", and"
            if i == len(endpoints.keys()) - 1:
                end_str += "."
            else:
                end_str += "; "

        self.hint_text = f"{hint_str}{end_str}"

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



