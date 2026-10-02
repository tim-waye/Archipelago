import logging
from dataclasses import dataclass
from typing import Dict, Optional, Set, Callable, Tuple, Iterable

from BaseClasses import Item, Region, Location, CollectionState, MultiWorld
from worlds.archilogic.errors import ArchiLogicError

@dataclass(frozen=True)
class CustomEndpoint:
    name: str
    player: int
    code: None = None        # Just here so flatten_item can be called on a CustomEndpoint
    is_event: bool = True    # Not a collectible item, so we treat it like an event to filter it out

    items: Tuple[str, ...] = ()                 # Names of required items to satisfy this endpoint
    locations: Tuple[str, ...] = ()             # Names of locations which must be accessible

    # Any other custom rule that can be determined from a CollectionState. Note that this rules will be evaluated
    # repeatedly by ArchiLogic to determine the minimum set of items that are strictly required to make it return true.
    # If this rule can't be completed for a given seed, the whole CustomEndpoint *will* be dropped, so be warned.
    rule: Optional[Callable[[CollectionState], bool]] = None

    @property
    def hint_text(self) -> str:
        return self.name

    def __post_init__(self):
        if not (self.items or self.locations or self.rule):
            raise ArchiLogicError(f"CustomEndpoint {self.name!r} defines no items, locations, collectibles or rules")

Endpoint = Item | CustomEndpoint

class GameProfile:
    """Per-game overrides for default ArchiLogic behavior. Subclass and set `game` to the AP game name."""
    game: str = ""
    PROFILES: Dict[str, "GameProfile"] = {}

    # Map of item, event, location, or region names to a preferred name to display.
    # Use this if your internal names or hint_text fields aren't ideal for player-facing text
    display_names: Dict[str, str] = {}

    # Use this to toggle whether ArchiLogic should default to an object's hint_text field over its name field when
    # generating player-facing text. The only accepted keys will be "item", "location", and "region"
    prefer_hint_text: Dict[str, bool] = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.game:
            GameProfile.PROFILES[cls.game] = cls()

    def display_name(self, obj: Item | Location | Region) -> str:
        """Returns any entry in display_names for an object. If none exists returns the result of preferred_label."""
        display_name = self.display_names.get(obj.name)
        return display_name if display_name else self.preferred_label(obj)

    def preferred_label(self, obj: Item | Location | Region) -> str:
        """
        # Returns hint_text or name for an object based on profile settings. Defaults to hint_text if unset.
        # Also note that display names will always override both fields if they exist
        """
        key = ("item" if isinstance(obj, Item)
               else "location" if isinstance(obj, Location)
               else "region")
        return obj.hint_text if self.prefer_hint_text.get(key, True) else obj.name

    def adjust_endpoints(self, endpoints: Set[Item], multiworld: MultiWorld, player: int) -> Set[Endpoint]:
        """Override to add, remove or replace ArchiLogic's computed endpoints."""
        return endpoints

    def items_from_names(self, multiworld: MultiWorld, player: int, names: Iterable[str]) -> Set[Item]:
        """Resolve item names to this player's Item objects, wherever they were placed in the multiworld."""
        wanted_items = set(names)
        found_items: Set[Item] = set()
        for loc in multiworld.get_filled_locations():
            item = loc.item
            if item is not None and item.player == player and item.name in wanted_items:
                found_items.add(item)
        if missing := wanted_items - {item.name for item in found_items}:
            logging.warning("%s profile: unknown item names %s", self.game, sorted(missing))
        return found_items


DEFAULT = GameProfile()

def profile_for(game: str) -> GameProfile:
    return GameProfile.PROFILES.get(game, DEFAULT)



