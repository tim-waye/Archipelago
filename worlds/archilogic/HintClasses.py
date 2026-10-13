import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Dict, Tuple, Set, Optional

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

class Certainty(StrEnum):
    """How strong a claim a path membership supports."""
    Necessary = "necessary"        # required under every route — removing it breaks the endpoint
    Route = "route"                # on the cheapest route; the player may find another way
    BulkCluster = "bulk_cluster"   # reaching the cheapest copies of an N-of-M gate

# Explicit because StrEnum orders alphabetically, which would rank BulkCluster strongest.
CERTAINTY_RANK: Dict[Certainty, int] = {
    Certainty.BulkCluster: 0,
    Certainty.Route: 1,
    Certainty.Necessary: 2,
}

@dataclass
class PathMember:
    """One item's membership on one path, with how many copies the route needs and how strong
    a claim that is. A path is Dict[Item, PathMember] so repeat arrivals can strengthen it."""
    item: Item
    count: int = 1
    certainty: Certainty = Certainty.Route

def flatten_item(item: Item) -> dict:
    return {"name": get_display_name(item), "id": int_id(item.code), "player": item.player}

class Hint:
    hint_type: HintType

    def __init__(self, hint_item: Item, directness: Directness):
        self.id: Optional[int] = None
        self.item = hint_item
        self.item_id = int_id(hint_item.code)
        self.item_player = hint_item.player
        self.location_id = int_id(hint_item.location.address) if hint_item.location else None
        self.location_player = hint_item.location.player if hint_item.location else None
        self.finder_id = hint_item.player if hint_item.location is None else hint_item.location.player
        # First half of hint text.
        self.subject_parts, self.directness = write_item_hint_text(hint_item, directness)
        self.parts: list[dict] = []

    @property
    def hint_text(self) -> str:
        return "".join(part["text"] for part in self.parts)

    def to_slot_data(self) -> dict:
        return {
            "id": self.id,
            "hint_text": self.hint_text,
            "parts": self.parts,
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
            woth_players: Dict[int, str],
            all_players: bool,
    ):
        super().__init__(hint_item, directness)
        self.woth_player_ids = set(woth_players.keys())

        # Write hint text. First half of the string is already initialized in the super constructor
        parts = self.subject_parts + [span("text", "is on the Path to ")]
        if all_players:
            parts.append(span("player", "Everybody's "))
        parts.append(span("text", "Victory"))

        if not all_players and len(woth_players) > 0:
            self.hint_type = HintType.SpecificWoth
            parts.append(span("text", " for "))
            parts += concat_list(
                [[span("player", name, slot)]
                 for slot, name in sorted(woth_players.items())],
                ',', True,
            )
        else:
            self.hint_type = HintType.GenericWoth

        parts.append(span("text", "."))
        self.parts = parts

    def to_slot_data(self) -> dict:
        slot_data = super().to_slot_data()
        slot_data["hint_type"] = self.hint_type.value
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
        self.parts = (
            self.subject_parts
            + [span("text", "is on the Path to ")]
            + [span("endpoint", get_display_name(endpoint), endpoint.player)]
            + [span("text", ".")]
        )

    def to_slot_data(self) -> dict:
        slot_data = super().to_slot_data()
        slot_data["hint_type"] = self.hint_type.value
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
        groups = []
        for (slot, player_name), ends in endpoints.items():
            end_spans = [
                [span("endpoint", get_display_name(end), end.player)]
                for end in sorted(ends, key=get_display_name)
            ]
            groups.append(
                concat_list(end_spans, ',', True)
                + [span("text", " for "), span("player", player_name, slot)]
            )

        groups.sort(key=lambda g: "".join(p["text"] for p in g))

        self.parts = (
            self.subject_parts
            + [span("text", f"is on the Path to{":\n" if len(groups) > 1 else " "}")]
            + concat_list(groups, ';\n', True)
            + [span("text", ".")]
        )

    def to_slot_data(self) -> dict:
        slot_data = super().to_slot_data()
        slot_data["hint_type"] = self.hint_type.value
        slot_data["endpoints"] = [
            flatten_item(e)
            for _, ends in sorted(self.endpoints.items())
            for e in sorted(ends, key=get_display_name)
        ]
        return slot_data

def write_item_hint_text(item, directness) -> Tuple[list[dict], Directness]:
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
            return [span("item", get_display_name(item), item.player),
                    span("text", " ")], direct
        case Directness.Location:
            if item.location:
                return [span("text", "Something "),
                        span("location", get_display_name(item.location), item.location.player),
                        span("text", " ")], direct
            else:
                raise ArchiLogicError("Tried to make a Location path hint for an item with no location")
        case Directness.Region:
            if item.location and item.location.parent_region:
                return [span("text", "Something in "),
                        span("region", get_display_name(item.location.parent_region), item.location.player),
                        span("text", " ")], direct
            else:
                raise ArchiLogicError("Tried to make a Region path hint for an item with no parent region")
    raise ArchiLogicError(f"Unable to write hint text for item: {get_display_name(item)}")

def concat_list(parts: list[list[dict]], delimiter: str, oxford: bool) -> list[dict]:
    if not parts:
        return []
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return parts[0] + [span("text", f"{delimiter}{"and " if "\n" in delimiter else " and "}")] + parts[1]

    out: list[dict] = []
    for part in parts[:-1]:
        if out:
            out.append(span("text", f"{delimiter} "))
        out += part
    out.append(span("text", f"{delimiter if oxford else ''} and "))
    out += parts[-1]
    return out

def span(kind: str, text: str, player: int | None = None) -> dict:
    return {"kind": kind, "text": text, "player": player}

def int_id(value) -> Optional[int]:
    """SOME apworlds (not pointing fingers) store non-id values in their objects' address or code fields.
    Those aren't useful ids for us, so we'll treat them as unaddressed."""
    return value if isinstance(value, int) else None
