import heapq
import itertools
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Any, Callable
from collections import Counter, defaultdict

import math

from BaseClasses import CollectionState, Location, Item, ItemClassification, DEFAULT_COLLECTION_RULE, Region
from NetUtils import convert_to_base_types
from Options import OptionError
from worlds.AutoWorld import World
from worlds.archilogic.HintClasses import (MultiPathHint, Directness, SinglePathHint, WothHint, Hint,
                                           set_player_profiles, Certainty, CERTAINTY_RANK, PathMember, get_display_name)
from worlds.archilogic.errors import ArchiLogicError
from worlds.archilogic.games import GameProfile, profile_for, CustomEndpoint


class ArchiLogicWorld(World):
    game = "ArchiLogic"
    item_name_to_id: Dict[str, int] = {}
    location_name_to_id: Dict[str, int] = {}

    @classmethod
    def stage_assert_generate(cls, multiworld) -> None:
        slots = multiworld.get_game_players(cls.game)
        if len(slots) > 1:
            raise OptionError(
                f"ArchiLogic supports only one slot per multiworld. Please remove extra ArchiLogic yaml")

    # TODO make these overrideable from options in the yaml. Per game preferably
    bulk_item_threshold = 6
    locks_too_much_threshold = 50

    # Debug artifacts written into the per-run analysis directory.
    dump_paths = True                # endpoint paths — the main tuning artifact
    dump_profile_reference = False   # names, groups and events, for writing a GameProfile

    # Making the paths feel good and useful is going to be a bit of a tuning exercise. I've made a few features to
    # filter what is eligible for path hints, but it's going to take some trial and error to figure out what's best.
    # So I've but them behind these feature flags for now. Maybe in the future these will be configurable through
    # the player yaml or something.

    # Filters Items which are events off of paths. Events themselves aren't typically shuffled and are more part of
    # the game's internal workings. We want paths to just hint towards actual items.
    # Note: we still allow Events to be Endpoints, because these events are likely to be understood by players since
    # they are the game's victory conditions. Things like "Final Boss Defeated", or "Final Level Completed".
    filter_events = True

    # If any endpoint doesn't have any path hintable requirements then drop that endpoint off the list of paths.
    # Also captures which endpoints this happens to so they can be hinted in other ways if needed.
    filter_empty_paths = True

    # For each of a players paths, removes any other endpoints and their path reqs so that all paths don't just become
    # nested within each other. The goal is for paths to focus on the unique requirements for its endpoint.
    # This is inspired by DK64R's hint logic where things like level entry is assumed. Otherwise, every item in later
    # levels would require the full path to the key that unlocked that level. Which isn't really helpful for solving
    # the specific path/location requirements we're trying to give a hint for.
    filter_other_endpoints = True

    # If any game has a single endpoint after we calculate what solves its completion condition, we dig deeper into that
    # endpoint's requirements until we have multiple at once, which we'll use as the game's endpoints instead.
    # This is because some apworlds use a single item or event collected at the end of the game to trigger the
    # completion condition. Some even have semi-artificial items meant to mark completion (Banana Hoard in DK64).
    # But having a single endpoint defeats the purpose of path hints, because all required items will just be required
    # for the same one path. Indistinguishable from WOTH hints and loses the extra info of path hints.
    expand_single_endpoints = True

    # Any item that has multiple copies with the same name, but not enough to be considered bulk, is treated as
    # ineligible for path hints. This is because they are prone to creating either/or situations where we can't say for
    # certain which copies of the item are strictly required for completing the path. There could be some more complex
    # handling done here, but for now I'm just going to sidestep the issue until a later update.
    filter_multi_copy_items = True

    # If any items are forced early either within one game or the whole multiworld, exclude them from the paths
    # These items are most likely forced early because they lock a large proportion of some game's locations. If one
    # item locks most of the checks in a world, then it'll will practically always be required, probably will be on
    # every path for that world, AND on every other player's path if ANY of their required items are in this world.
    #
    # An example of this is Dark Souls 3's Small Lothric Banner, which gates you from leaving the first area of the game.
    # If we path hinted it, it'd be like saying "some items on all these paths are somewhere in the ~95% of DS3 that is
    # behind the Small Lothric Banner."
    # Which is not very helpful to anyone, we're better off just hinting by world at that point.
    filter_forced_early = True

    # If any one item locks too much of its own game, then leave it off of paths. This is configurable by setting the
    # locks_too_much_threshold value. Anything locking more than that percentage of checks will be considered too much.
    # The reasoning for this is the same as filter_forced_early, widely required items are likely to be found early,
    # and don't provide much useful information for path hints.
    filter_locks_too_much = True

    # Flags for Hint creation
    create_generic_woths = False
    create_specific_woths = True
    create_single_paths = False
    create_multi_paths = True

    # Flag for building item_requirements for the hint_towards command
    build_item_requirements = True

    # Economy. hint_cost and location_check_points are host settings chosen when the room
    # is launched, not at generation, so ArchiLogic paces against each player's own
    # location count instead: one hint per grant_percent of their checks.
    hints_per_player = 20
    grant_percent = 3
    toward_percent = 5
    location_counts: Dict[str, int] = {}

    selected_hints: List[Hint] = []
    location_graph: List[dict] = []
    starting_items: Dict[str, Dict[str, int]] = {}

    def fill_slot_data(self) -> Dict[str, Any]:
        return {
            "hints": [hint.to_slot_data() for hint in self.selected_hints],
            "locations": self.location_graph,
            "starting_items": self.starting_items,
            "bulk_item_threshold": self.bulk_item_threshold,
            "location_counts": self.location_counts,
            "hint_economy": {
                "grant_percent": self.grant_percent,
                "toward_percent": self.toward_percent,
            },
        }

    @classmethod
    def stage_pre_output(cls, multiworld):
        logging.info("Entered Stage Pre-Output for ArchiLogic.")
        t0 = time.perf_counter()

        # Initialize some helper objects for later
        all_players = [player for player in multiworld.player_ids if multiworld.game[player] != cls.game]
        archilogic_world = multiworld.worlds[multiworld.get_game_players(cls.game)[0]]
        set_player_profiles({p: multiworld.game[p] for p in multiworld.player_ids})
        logging.info("ArchiLogic profiles loaded: %s", sorted(GameProfile.PROFILES))


        # Setup debug logging and json dumping
        here: str = os.path.dirname(__file__)
        run_dir = os.path.join(here, "analysis",
                               f"{time.strftime('%Y%m%d-%H%M%S')}-seed{getattr(multiworld, 'seed', 'x')}")
        os.makedirs(run_dir, exist_ok=True)

        run_log = logging.FileHandler(os.path.join(run_dir, "archilogic.log"), encoding="utf-8")
        run_log.setFormatter(logging.Formatter("%(levelname)s  %(message)s"))
        logging.getLogger().addHandler(run_log)

        # Step One: In order to calculate the critical paths for the multiworld and/or each player, we essentially need our
        # own version of the playthrough from the spoiler log. So we're going to copy how the Spoiler class does that.
        logging.info("Building Playthrough to calculate critical paths.")

        # Need a set of locations holding progression items. We'll also keep a count of these items for later.
        prog_locations = set()
        item_counts: Counter[Tuple[str, int]] = Counter()
        for loc in multiworld.get_filled_locations():
            item_counts[(loc.item.name, loc.item.player)] += 1
            if loc.item.advancement:
                prog_locations.add(loc)
        item_counts += Counter((i.name, i.player) for items in multiworld.precollected_items.values()
                               for i in items if i.advancement)

        state_cache: List[Optional[CollectionState]] = [None]
        state = CollectionState(multiworld)
        sphere_candidates = set(prog_locations)

        # Some apworlds create a second, code-less copy of an item to seed a progressive chain.
        # Ship of Harkinian precollects event copies of Progressive Scale, Stick Capacity, Nut
        # Capacity and Wallet when their shuffle option is off, so the first real copy upgrades
        # you to the second tier instead of the first. Item hashes on (name, player), so those
        # collapse onto the real copies and a requirement set holds whichever was inserted first.
        # Anything reading a per-instance attribute — code, location, is_event — needs the placed
        # copy, not whichever instance the set happened to capture.
        canonical_items: Dict[Item, Item] = {}
        for loc in multiworld.get_filled_locations():
            if not loc.item.is_event:
                canonical_items.setdefault(loc.item, loc.item)

        def canonical(item: Item) -> Item:
            """The placed copy of this item. Falls back to the given instance for genuine events
            and items that exist only in someone's starting inventory."""
            return canonical_items.get(item, item)

        # Data sets we'll build hints from later
        collection_spheres: List[Set[Location]] = []
        go_mode_sphere: Dict[int, int] = {}

        # Stage One: build the spheres
        while sphere_candidates:
            sphere = {location for location in sphere_candidates if state.can_reach(location)}

            for loc in sphere:
                state.collect(loc.item, True, loc)

            sphere_candidates -= sphere
            collection_spheres.append(sphere)
            state_cache.append(state.copy())

            logging.debug('Calculated sphere %i, containing %i of %i progress items.', len(collection_spheres),
                          len(sphere),
                          len(prog_locations))
            if not sphere:
                if not multiworld.has_beaten_game(state):
                    raise RuntimeError("During playthrough generation, the game was determined to be unbeatable. "
                                       "Something went terribly wrong here. "
                                       f"Unreachable progression items: {sphere_candidates}")
        t1a = time.perf_counter()

        # Stage Two: prune spheres down to only the required locations.
        required_locations = {location for sphere in collection_spheres for location in sphere}
        for num, sphere in reversed(tuple(enumerate(collection_spheres))):
            to_delete: Set[Location] = set()
            for loc in sorted(sphere):
                required_locations.remove(loc)
                if multiworld.can_beat_game(state_cache[num], required_locations):
                    to_delete.add(loc)
                else:
                    required_locations.add(loc)
            sphere -= to_delete
        t1b = time.perf_counter()

        # Stage Three: Repair any unintentional links that may have been made in step two.
        # One last forward collection iteration through the state.
        required_locations = {item for sphere in collection_spheres for item in sphere}
        state = CollectionState(multiworld)
        collection_spheres = []
        while required_locations:
            sphere = set(filter(state.can_reach, required_locations))

            for loc in sphere:
                state.collect(loc.item, True, loc)

            collection_spheres.append(sphere)

            for player in all_players:
                if player not in go_mode_sphere and multiworld.completion_condition[player](state):
                    go_mode_sphere[player] = len(collection_spheres) - 1

            required_locations -= sphere
            if not sphere:
                raise RuntimeError(f'Not all required items reachable. Unreachable locations: {required_locations}')

        # Which sphere each item first becomes obtainable in. Used to resolve either/ors toward
        # the branch that opens earliest, rather than whatever Item.__lt__ happens to sort first.
        # setdefault keeps the earliest sphere for multi-copy items, since Item hashes on
        # (name, player) and all copies collapse to one key.
        item_sphere_depth: Dict[Item, int] = {}
        for depth, sphere in enumerate(collection_spheres):
            for location in sphere:
                item_sphere_depth.setdefault(location.item, depth)

        # Which sphere each location opens in. Choosing between copies of one item needs
        # per-location depth, which the item-keyed map above can't express.
        location_sphere_depth: Dict[Location, int] = {}
        for depth, sphere in enumerate(collection_spheres):
            for location in sphere:
                location_sphere_depth[location] = depth

        logging.info("Finished calculating collection_spheres for ArchiLogic.")
        t1 = time.perf_counter()

        # Step Two: Calculate per-player requirements.
        # player_req_locs will be a set of locations that must be checked for each player to beat their game
        # Note: these required locations include not just progression items, but also any events, minor items, or collecting
        # of currency that an apworld tracks as a Location internally. So this isn't the same thing as a player's go mode items.
        player_req_locs: Dict[int, Set[Location]] = {}

        # Function to determine whether a location is required for a specific player to beat their game
        def can_player_beat(multiworld, player_id: int, locs: set[Location]):
            current_state = CollectionState(multiworld)
            if multiworld.completion_condition[player_id](current_state):
                return True
            for _ in current_state.sweep_for_advancements(locs, yield_each_sweep=True,
                                                  checked_locations=current_state.locations_checked):
                if multiworld.completion_condition[player_id](current_state):
                    return True
            return False

        # Loop through all required locations to determine which are required for each player.
        global_req_locs: Set[Location] = {loc for sphere in collection_spheres for loc in sphere}
        for player in all_players:
            logging.info("Determining which locations are needed to beat Player %d's game.", player)
            req = set(global_req_locs)
            for loc in sorted(global_req_locs):
                req.remove(loc)
                if not can_player_beat(multiworld, player, req):
                    req.add(loc)
            player_req_locs[player] = req

        # Sanity check
        assert global_req_locs == set().union(*player_req_locs.values())

        logging.info("Finished calculating required locations per player")
        t2 = time.perf_counter()

        # Step Three: Calculate per-world region and location requirements

        # Precollected items can be required, so start with a state that has no advancement items collected.
        baseline = CollectionState(multiworld)
        for items in multiworld.precollected_items.values():
            for item in items:
                if item.advancement: baseline.remove(item)

        # Here's where things get interesting. This function returns two sets of items for each location:
        # - access_reqs: a minimum viable set of items needed to access the given location or region
        # - min_reqs: a subset of access_reqs containing only items that we can guarantee are required
        #
        # min_reqs is not necessarily the same as access_reqs because of what we'll call "either/ors".
        # This is a situation where something has multiple valid combinations of items that can gate access.
        # (Shoutout to the DK64R Community for teaching me all about this!)
        #
        # For example, our location is a locked chest in a temple. The only way to open the chest is with a key. So we
        # know this key is always required. But there are two different ways to get into the temple. The first option is
        # to use a bomb to blow open the debris covering the entrance. Or we could give a wizard a magic gem and a
        # crystal orb and in return he'll teleport us inside.
        # So in this case access_reqs has two valid values: {Chest Key, Bombs} or {Chest Key, Gem, Orb}
        # But, min_reqs will only be the item that is known to ALWAYS be required: {Chest Key}
        #
        # If an access_reqs set does contain an either/or, we only end up keeping one of the valid combinations.
        # Which one isn't arbitrary though — we test the latest-available items for removal first, so whatever
        # survives is the branch that opens earliest. That's the route a player is most likely to take anyway.
        # We don't need to enumerate every possible combination, because we're not trying to hide the either/or
        # anymore. The items this route picked that min_reqs didn't get flagged as uncertain later. (Step Four.)
        def get_access_reqs(reaches: Callable[[CollectionState], bool], candidates: List[Item], label: str = ""
                            ) -> Tuple[Set[Item], Set[Item]]:
            access_reqs: Set[Item] = set()
            min_reqs: Set[Item] = set()
            counts = Counter(candidates)

            if not reaches(baseline):
                # Make a state object with all our candidates collected so we can test if each one is needed
                state = baseline.copy()
                for item in candidates:
                    state.collect(item, True)
                assert reaches(state), f"{label} unreachable with every candidate held"
                access_reqs = set(counts)

                # Remove all of each item from the state and test if we can still reach the target.
                # If so we discard the item(s) from our needed set, because they aren't needed.
                # If removing them means we can't reach the target anymore we re-collect them and keep them in the needed set
                # Discard the latest-available items first, so what survives is the branch that
                # opens earliest. Precollected items default to -1 and are tested last, which is
                # right — they're free, so keeping them is always the cheaper route.
                for item in sorted(counts, key=lambda i: (-item_sphere_depth.get(i, -1), i.name)):
                    n = counts[item]
                    for _ in range(n):
                        state.remove(item)
                    if reaches(state):
                        access_reqs.discard(item)
                    else:
                        for _ in range(n):
                            state.collect(item, True)

            if not access_reqs:
                return set(), set()

            # Now that we have a value for access_reqs we can determine the min_reqs
            # First, reset our state
            state = baseline.copy()
            for item in candidates:
                state.collect(item, True)

            # Now test if we can still reach the target once all of an item is removed from the state.
            # This differs from the previous loop because we only loop over the needed set of items, and we re-collect
            # each item whether it was determined to be required or not.
            for item in access_reqs:
                n = counts[item]
                for _ in range(n):
                    state.remove(item)
                if not reaches(state):
                    min_reqs.add(item)
                for _ in range(n):
                    state.collect(item, True)

            return access_reqs, min_reqs

        # A set of items that let a player access each region and location specifically
        region_reqs: Dict[Region, Set[Item]] = {}
        location_reqs: Dict[Location, Set[Item]] = {}

        # The minimum set of items that we can guarantee are required (logically) to access a region or location
        region_necessary: Dict[Region, Set[Item]] = {}
        location_necessary: Dict[Location, Set[Item]] = {}

        # Now we loop through the spheres for each player, determine the target locations, then each location's reqs
        for player in all_players:
            logging.info("Determining location access requirements for Player %d's game.", player)

            for sphere_index, sphere in enumerate(collection_spheres):
                targets = {loc for loc in sphere if loc.player == player}
                if not targets: continue

                # Candidates for required items will be either be items in earlier spheres or in starting inventory.
                candidates = [loc.item for earlier_sphere in collection_spheres[:sphere_index]
                              for loc in earlier_sphere if loc.item.player == player]
                candidates += [i for i in multiworld.precollected_items[player] if i.advancement]

                # Start by determining region access rules.
                for region in {loc.parent_region for loc in targets}:
                    logging.debug("Determining access requirements for region %s", region.name)
                    if region in region_reqs: continue
                    region_reqs[region],  region_necessary[region] = get_access_reqs(
                        region.can_reach, candidates, region.name)

                # Now determine location-specific access rules.
                for loc in targets:
                    logging.debug("Determining access requirements for location %s", loc.name)
                    base_region_reqs = region_reqs[loc.parent_region]

                    loc_state = baseline.copy()
                    for item in base_region_reqs:
                        loc_state.collect(item, True)
                    if loc.access_rule(loc_state):
                        # This location is accessible as soon as its parent region is.
                        location_reqs[loc] = base_region_reqs
                        location_necessary[loc] = region_necessary[loc.parent_region]
                    else:
                        location_reqs[loc], location_necessary[loc] = get_access_reqs(
                            loc.can_reach, candidates, loc.name)

        logging.info("Finished calculating access requirements.")
        t3 = time.perf_counter()

        # Step Four:  Resolve any Events in requirement sets into their own required items
        # Players are more likely to think of progression in terms of items than events.
        def resolve_event_reqs(reqs, reqs_by_location, cache):
            """Replace event requirements with whatever gates them, recursively.

            Memoised on set contents rather than location, so a region and every
            default-rule location sharing its set resolve once between them.

            `reqs_by_location` is where the recursion looks up an event's own
            requirements — always location-keyed, since events live at locations.
            """
            key = frozenset(reqs)
            if key in cache:
                return cache[key]
            out = set()
            for item in reqs:
                if item.is_event and item.location is not None:
                    assert item.location in reqs_by_location, \
                        f"{item.location} is referenced as a requirement but not in the reqs set!"
                    out |= resolve_event_reqs(reqs_by_location[item.location], reqs_by_location, cache)
                else:
                    out.add(item)
            cache[key] = frozenset(out)
            return cache[key]

        # Base reqs and necessary reqs need their own caches because they may resolve the same location differently.
        reqs_cache: Dict[frozenset, frozenset] = {}
        resolved_loc_reqs: Dict[Location, Set[Item]] = \
            {loc: resolve_event_reqs(location_reqs[loc], location_reqs, reqs_cache) for loc in location_reqs}
        resolved_region_reqs: Dict[Region, Set[Item]] = \
            {r: resolve_event_reqs(region_reqs[r], location_reqs, reqs_cache) for r in region_reqs}

        necessary_cache: Dict[frozenset, frozenset] = {}
        resolved_loc_necessary: Dict[Location, Set[Item]] = \
            {loc: resolve_event_reqs(location_necessary[loc], location_necessary, necessary_cache)
             for loc in location_necessary}
        resolved_region_necessary: Dict[Region, Set[Item]] = \
            {r: resolve_event_reqs(region_necessary[r], location_necessary, necessary_cache)
             for r in region_necessary}

        # Now we can most efficiently calculate which locations have either/or requirements, and which items only
        # ever show up in one.
        #
        # Remember that access_reqs is one route that works, while min_reqs is only what EVERY route needs. So the
        # difference between the two is exactly the set of items this particular route happened to pick. Back to our
        # temple: access_reqs might be {Chest Key, Bombs}, min_reqs is {Chest Key}, so the Bombs are the either/or.
        #
        # I used to strip these off of paths entirely, since saying "the Bombs are required" is a lie when the wizard
        # would have worked just as well. But that threw away real information, because the Bombs ARE required on the
        # route we actually walked. So now the walk keeps them and labels them instead. Anything that's necessary at
        # the location which pulled it in stays Certainty.Necessary, and anything this route merely chose gets demoted
        # to Certainty.Route. (See walk_requirements for where that happens.)
        #
        # So our temple's Chest Key is Necessary and the Bombs are Route. We can hint either one, but only the Chest
        # Key can be stated as unavoidable.
        #
        # What's left below is just a diagnostic now. The tally at the end of the run reports how many either/or
        # locations we found and how many of them ended up on a path.
        either_or_locs: Dict[Location, Set[Item]] = {}
        for loc in resolved_loc_reqs:
            known_either_or_items = resolved_loc_reqs[loc] - resolved_loc_necessary[loc]
            if known_either_or_items:
                either_or_locs[loc] = known_either_or_items

        either_or_regions: Dict[Region, Set[Item]] = {}
        for reg in resolved_region_reqs:
            known_either_or_items = resolved_region_reqs[reg] - resolved_region_necessary[reg]
            if known_either_or_items:
                either_or_regions[reg] = known_either_or_items
        t4 = time.perf_counter()

        # Step Five-and-a-half: the location requirement graph that paths and /hint_toward share.
        #
        # Built from access_reqs, not min_reqs. min_reqs holds only what's required under EVERY
        # route, so slack destroys it — an endpoint gated by "50 of 80 Dragons" has no necessary
        # requirements at all, and used to yield a path of one item. access_reqs is one complete
        # viable route, which always exists. min_reqs is kept, as the certainty tier.
        requirements_start = time.perf_counter()

        def copies_required(target_location, loc_requirements, available_items) -> Counter:
            """
            How many copies of each requirement the target location actually needs.
            This scan accounts for items with multiple copies (think Progressive Strength from OOT)
            We do this by binary searching on the total amount of copies to determine exactly how many copies
            a specific location requires. This is also needed to determine when multiple copies of an item
            create an either/or situation, where multiple copies are equally valid options for progression.
            """
            available_copies = Counter(available_items)
            required_copies = Counter()

            for req in loc_requirements:
                total_copies = available_copies[req]
                if total_copies <= 1:
                    required_copies[req] = 1
                    continue

                state_without_reqs = baseline.copy()
                for item in available_items:
                    if item != req:
                        state_without_reqs.collect(item, True)

                fewest, most = 1, total_copies
                while fewest < most:
                    trial_count = (fewest + most) // 2
                    probe_state = state_without_reqs.copy()
                    for _ in range(trial_count):
                        probe_state.collect(req, True)
                    if target_location.can_reach(probe_state):
                        most = trial_count
                    else:
                        fewest = trial_count + 1
                required_copies[req] = fewest

            return required_copies

        all_available_items = [loc.item for loc in global_req_locs]
        all_available_items += [item for items in multiworld.precollected_items.values()
                                for item in items if item.advancement]

        # Keyed by location, not by item. Sphere ordering guarantees a location only requires
        # items from earlier spheres, which makes the graph acyclic — but that holds per
        # location, not per item type. Collapsing copies lets a deep copy's requirements stand
        # in for a shallow one, which closes cycles: Sublime Bone Dust -> King's Ring ->
        # Strength Upgrade -> Lens of Truth -> Sublime Bone Dust.
        location_req_counts: Dict[Location, Counter] = {}
        location_contents: Dict[Location, Item] = {}

        for location in global_req_locs:
            if location.item is None:
                continue
            location_contents[location] = location.item
            location_req_counts[location] = Counter(
                {canonical(req): count for req, count
                 in copies_required(location, resolved_loc_reqs.get(location, set()),
                                    all_available_items).items()})

        # Precollected items have no location, so they're the client's starting state rather
        # than nodes in the graph.
        starting_items: Dict[int, Counter] = {
            player: Counter(item.name for item in items if item.advancement)
            for player, items in multiworld.precollected_items.items()
        }

        # Every location holding a copy of each item, cheapest sphere first.
        item_holders: Dict[Item, List[Location]] = defaultdict(list)
        for location, item in location_contents.items():
            item_holders[item].append(location)
        for locations in item_holders.values():
            locations.sort(key=lambda loc: (location_sphere_depth.get(loc, len(collection_spheres)),
                                            loc.player, loc.name))

        # Precollected copies are free: already held, and they cost no path to reach.
        free_copies: Counter[Item] = Counter()
        for items in multiworld.precollected_items.values():
            for item in items:
                if item.advancement:
                    free_copies[item] += 1

        graph_copies: Counter[Item] = Counter(free_copies)
        for item in location_contents.values():
            graph_copies[item] += 1

        def cheapest_holders(item, count) -> List[Location]:
            """The locations holding the `count` cheapest copies. Precollected copies come first,
            so only the shortfall needs locations.

            Counting them is required for soundness, not an optimisation: skipping them reaches
            past a starting copy into a deeper one and invents a dependency the route doesn't
            have. Measured on a 5-game seed, 13 of 200 N-of-M gates were wrong without it, all
            Progressive Wallet — 2 holder locations plus 1 starting copy, so a sphere-zero shop
            that needs one wallet got told it needed a sphere-one location."""
            shortfall = count - free_copies[item]
            return item_holders[item][:shortfall] if shortfall > 0 else []

        logging.info("Built location requirements in %.2fs: %d locations",
                     time.perf_counter() - requirements_start, len(location_req_counts))

        # Step Five: Determine which items and location are worth giving hints for.

        def is_forced_early(item):
            if not cls.filter_forced_early:
                return False
            return (item.name in multiworld.early_items.get(item.player, {})
                    or item.name in multiworld.local_early_items.get(item.player, {}))

        # Some games have single items that lock off a large percentage of their checks. This makes the item so
        # ubiquitous that in most seeds it would end up on any path that has any of its items in that game. Thus making
        # it not a very useful item to hint.
        # So if any item locks more than our set threshold we'll make it ineligible for path hinting.
        everything = CollectionState(multiworld)
        for loc in multiworld.get_filled_locations():
            everything.collect(loc.item, True)

        # Expensive: O(items × locations) can_reach calls. Lazy and memoized so it only runs
        # for items that pass every cheaper gate.
        reachable_by_player: Dict[int, List[Location]] = {}
        locks_cache: Dict[Tuple[str, int], bool] = {}

        def locks_too_much(item) -> bool:
            if not cls.filter_locks_too_much:
                return False
            key = (item.name, item.player)
            if key not in locks_cache:
                reachable = reachable_by_player.get(item.player)
                if reachable is None:
                    reachable = [loc for loc in multiworld.get_locations(item.player)
                                 if loc.can_reach(everything)]
                    reachable_by_player[item.player] = reachable
                if not reachable:
                    locks_cache[key] = False
                else:
                    probe = everything.copy()
                    probe.remove(item)
                    locked = sum(1 for loc in reachable if not loc.can_reach(probe))
                    locks_cache[key] = 100 * locked / len(reachable) >= cls.locks_too_much_threshold
            return locks_cache[key]

        # An item is refused at some locations and admitted at others — Progressive Hookshot is
        # interchangeable at 37 locations and nameable at 4 — so a refusal only matters when the
        # item is never admitted anywhere.
        refusals: Dict[Tuple[str, int], Set[str]] = defaultdict(set)
        admitted: Set[Tuple[str, int]] = set()

        def is_path_member(item, count) -> bool:
            """Structurally a dependency of this route. Traversal is separate again — bulk gates
            are walked through without being members."""
            def refuse(reason):
                refusals[(item.name, item.player)].add(reason)
                return False

            total = item_counts[(item.name, item.player)]
            canon = canonical(item)
            if not canon.advancement:
                return refuse(f"classification {canon.classification.name}")
            if canon.location is None:
                return refuse("precollected")
            if cls.filter_multi_copy_items and 1 < total and count < total:
                return refuse(f"N of M (needs {count} of {total})")
            return True

        def is_hintable(item, record: bool = True) -> bool:
            """Eligible to be the subject of a hint. Presupposes membership. Count-independent,
            since the bulk gate keys off the total rather than what this route needs."""
            def refuse(reason):
                if record:
                    refusals[(item.name, item.player)].add(reason)
                return False

            total = item_counts[(item.name, item.player)]
            canon = canonical(item)
            if cls.filter_events and canon.is_event:
                return refuse("event")
            if canon.name in multiworld.worlds[canon.player].hint_blacklist:
                return refuse("hint_blacklist")
            if is_forced_early(canon):
                return refuse("forced_early")
            if profile_for(canon.game).item_is_excluded(multiworld, canon.name):
                return refuse("profile_excluded")
            if total > cls.bulk_item_threshold:
                return refuse(f"bulk ({total} copies > {cls.bulk_item_threshold})")
            if locks_too_much(canon):
                return refuse("locks_too_much")
            if record:
                admitted.add((canon.name, canon.player))
            return True

        t5 = time.perf_counter()

        # Returns a dict of item name to item for every required or precollected item for the given player
        def items_by_name(player):
            lookup = {}
            for loc in player_req_locs[player]:
                if loc.item.player == player:
                    lookup.setdefault(loc.item.name, loc.item)
            for item in multiworld.precollected_items[player]:
                if item.advancement:
                    lookup.setdefault(item.name, item)
            return lookup

        # Function used to handle items that get renamed by an apworld when collected
        # An example is LTTP internally stores "Progressive Sword" as "Fighter's Sword" or "Master Sword"
        stored_name_cache: Dict[Tuple[str, int], Optional[str]] = {}

        def stored_name(player, item):
            cache_key = (item.name, player)
            if cache_key not in stored_name_cache:
                probe = CollectionState(multiworld)
                before = Counter(probe.prog_items[player])
                probe.collect(item, True)
                after = probe.prog_items[player]
                changed = [k for k in after if after[k] > before.get(k, 0)]
                stored_name_cache[cache_key] = changed[0] if len(changed) == 1 else None
            return stored_name_cache[cache_key]

        # Tests which locations, if any, are required to be accessible by the game's completion condition.
        # Note: this is NOT the same as locations needing to be accessible in order to beat the game. This should only
        # determine if a game's completion condition rule (an arbitrary function defined in the apworld) DIRECTLY
        # enforces that some, or many locations are accessible.
        # For example, a game could have its completion condition be "Player can access post-game area"
        def goal_locking_locations(player, completed_state):
            locations = list(multiworld.get_locations(player))

            access_rule_backup = [loc.access_rule for loc in locations]
            try:
                # Force shut every location at once and test the completion condition. If it still passes then we know
                # it's not based on access rules, so we can get out of here.
                for loc in locations:
                    loc.access_rule = lambda state: False
                if multiworld.completion_condition[player](completed_state):
                    return set()
            finally:
                # Undo the forced access rules
                for loc, rule in zip(locations, access_rule_backup):
                    loc.access_rule = rule

            # If we're still here then completion does rely on some set of locations being accessible.
            # We'll repeat the test for individual locations and see which ones cause the condition to fail.
            required_locs = set()
            for loc in locations:
                original_rule = loc.access_rule
                loc.access_rule = lambda state: False
                try:
                    if not multiworld.completion_condition[player](completed_state):
                        required_locs.add(loc)
                finally:
                    loc.access_rule = original_rule
            return required_locs

        def endpoint_items(player, final_state):
            """
            Finds which items directly lock and/or represent the win condition for this player's game.
            Three ways an item qualifies, checked in order:
              1. it's read by a goal location's own access rule
              2. the goal fails without it while every goal location stays reachable, meaning the goal predicate
                 reads it directly
              3. it guards the nearest guarded entrance into a goal location's region
            """
            # two probes per item:
            #   deep    - caches cleared, so reachability is recomputed (test 2)
            #   shallow - caches intact, so only rules reading prog_items directly flip.
            #             This is what separates `state.has(X)` gates from `can_reach(...)`
            #             gates, which otherwise flip for every item on the path.
            _t0 = time.perf_counter()
            deep_state_cache = {}
            shallow_state_cache = {}
            for item in items_by_name(player).values():
                key = (item.name if item.name in final_state.prog_items[player]
                       else stored_name(player, item))
                count = final_state.prog_items[player].get(key, 0) if key else 0
                if not count:
                    continue

                state_minus_item = final_state.copy()
                for _ in range(count):
                    state_minus_item.remove(item)

                if multiworld.completion_condition[player](state_minus_item):
                    # Game is still beatable without this item. Skip it
                    continue

                deep_state_cache[item] = state_minus_item
                shallow = final_state.copy()
                shallow_backup = (shallow.reachable_regions[item.player],
                                    shallow.blocked_connections[item.player],
                                    shallow.stale[item.player])
                for _ in range(count):
                    shallow.remove(item)
                (shallow.reachable_regions[item.player],
                 shallow.blocked_connections[item.player],
                 shallow.stale[item.player]) = shallow_backup
                shallow_state_cache[item] = shallow

            reqs_for_goal = {i for i in deep_state_cache
                             if (i.is_event or not locks_too_much(i)) and not is_forced_early(i)
                             and item_counts[(i.name, i.player)] <= cls.bulk_item_threshold}

            if not reqs_for_goal:
                # Everything got filtered — better to report the raw set than nothing.
                reqs_for_goal = set(deep_state_cache)

            gate_cache = {}
            def item_opens_gate(spot, item):
                if spot not in gate_cache:
                    gate_cache[spot] = spot.access_rule(final_state)
                return gate_cache[spot] and not spot.access_rule(shallow_state_cache[item])

            found_endpoints = set()
            remaining_candidates = set()
            _t1 = time.perf_counter()
            goal_locs = goal_locking_locations(player, final_state)
            _t2 = time.perf_counter()
            logging.info("P%d probes %.2fs (%d kept) | goal_locking %.2fs (%d locs of %d)",
                         player, _t1 - _t0, len(deep_state_cache), _t2 - _t1,
                         len(goal_locs), len(multiworld.get_locations(player)))

            if not goal_locs:
                # The goal does not depend on access rules, so the items are the only endpoints
                found_endpoints = set(reqs_for_goal)
            else:
                for item in reqs_for_goal:
                    # Item is required to access a goal location
                    gates_a_goal_location = any(item_opens_gate(loc, item) for loc in goal_locs)

                    # Every goal location is still reachable without it, but completion condition is false
                    # without the item. So we must have required locations AND required items.
                    goal_reads_it = all(loc.can_reach(deep_state_cache[item]) for loc in goal_locs)

                    if gates_a_goal_location or goal_reads_it:
                        found_endpoints.add(item)
                    else:
                        remaining_candidates.add(item)

                # Now find the final entrance gating the end of the game. This gives us an endpoint instead of
                # EVERY entrance along the way.
                for gl in goal_locs:
                    seen_regions = set()
                    frontier = [gl.parent_region]
                    while frontier:
                        region = frontier.pop()
                        if region is None or region in seen_regions:
                            continue
                        seen_regions.add(region)
                        for entrance in region.entrances:
                            gates = set()
                            for candidate in remaining_candidates:
                                if item_opens_gate(entrance, candidate):
                                      gates.add(candidate)
                            if gates:
                                found_endpoints |= gates
                            elif entrance.access_rule(baseline):
                                frontier.append(entrance.parent_region)

            logging.info("P%d tests 1+2 caught: %s", player, sorted(i.name for i in found_endpoints))
            logging.info("P%d remaining:       %s", player, sorted(i.name for i in remaining_candidates))
            logging.info("P%d after test 3:    %s", player, sorted(i.name for i in found_endpoints))

            if not found_endpoints or found_endpoints == reqs_for_goal:
                found_endpoints = set(reqs_for_goal)

            # 4. One last edge case.
            # If a game only has one single endpoint then paths are no different from standard WOTH hints.
            # In this case we'll drill down until we find multiple requirements that unlock the goal condition.
            #
            # Read the UNRESOLVED requirements here. Step four swaps events out for the items behind them since hinting
            # items makes more sense than hinting events along the way. BUT for an endpoint, events will make sense.
            # If your goal condition is "beat the end boss" then my hope is, if it's not represented by getting some
            # item, or checking some location, then HOPEFULLY it would be a "beat the end boss" event.
            # (I guess we'll see how that works out. But it sounds good for now)
            if cls.expand_single_endpoints:
                seen_endpoint_items = set()
                while len(found_endpoints) == 1:
                    item = next(iter(found_endpoints))
                    if item in seen_endpoint_items or item.location is None:
                        break
                    seen_endpoint_items.add(item)
                    deeper = set()
                    for req in location_necessary.get(item.location, ()):
                        copies = item_counts[(req.name, req.player)]
                        if (req.is_event
                                or (is_hintable(req, record=False)
                                    and not (cls.filter_multi_copy_items and copies > 1))):
                            deeper.add(req)
                    deeper.discard(item)
                    if not deeper:
                        break
                    found_endpoints = deeper

            profile = profile_for(multiworld.game[player])
            adjusted = profile.adjust_endpoints(set(found_endpoints), multiworld, player)
            if adjusted and adjusted != found_endpoints:
                logging.info("P%d %s profile adjusted endpoints: %s -> %s", player, profile.game,
                             sorted(e.name for e in found_endpoints), sorted(e.name for e in adjusted))
                found_endpoints = adjusted

            logging.info("P%d endpoints:       %s", player, sorted(i.name for i in found_endpoints))
            return found_endpoints

        # Oh boy, finally time to build our paths!
        player_endpoints = {}
        go_mode_items: Dict[int, Set[Item]] = {}
        paths: Dict[int, Dict[Item, PathMember]] = {}

        # Keep track of which paths each item is on. This will be used to give path/multi-path hints.
        # The hints will be structured in some form of: "This item is on the path to these endpoints"
        items_to_paths = {}

        # Keep track of any endpoints that don't end up with useful paths to hint
        pathless_endpoints: Dict[int, Dict[Item, Set[Item]]] = defaultdict(dict)
        profile_dropped: Dict[CustomEndpoint, Set[Item]] = {}

        all_candidates = [loc.item for loc in global_req_locs]
        all_candidates += [i for i in multiworld.precollected_items[player] if i.advancement]

        # CustomEndpoints might have their own rules and need some special case handling.
        profile_reqs: Dict[CustomEndpoint, Set[Item]] = {}

        for player in all_players:
            paths[player] = {}
            player_endpoints[player] = endpoint_items(player, state)

            custom_endpoints = [end for end in player_endpoints[player] if isinstance(end, CustomEndpoint)]
            if custom_endpoints:
                required_items = [loc.item for loc in global_req_locs]
                required_items += [item for items in multiworld.precollected_items.values()
                                   for item in items if item.advancement]
                all_required_held = baseline.copy()
                for item in required_items:
                    all_required_held.collect(item, True)

                for endpoint in custom_endpoints:
                    # Case 1: CustomEndpoints can have a direct set of required items
                    wanted_items = set(endpoint.items)
                    reqs: Set[Item] = {loc.item for loc in multiworld.get_filled_locations()
                                       if loc.item.player == player and loc.item.name in wanted_items}
                    if missing := wanted_items - {item.name for item in reqs}:
                        logging.warning("P%d custom endpoint %r names unknown items: %s",
                                        player, endpoint.name, sorted(missing))


                    # Case 2: CustomEndpoints can have a set of locations that must be accessible
                    for loc_name in endpoint.locations:
                        location = next((l for l in multiworld.get_locations(player)
                                         if l.name == loc_name), None)
                        if location is None or location not in location_req_counts:
                            logging.info("P%d custom endpoint %r names %r, which is unknown or outside "
                                         "the required set.", player, endpoint.name, loc_name)
                            continue
                        reqs |= set(location_req_counts.get(location, Counter()))

                    # Case 3: CustomEndpoints can have a custom rule.
                    # This would be a function that takes a CollectionState and returns a bool.
                    rules = [endpoint.rule] if endpoint.rule is not None else []

                    # Now evaluate all rules, dropping the endpoint if any of them are impossible in this seed.
                    dropped_endpoint = False
                    for rule in rules:
                        if not rule(all_required_held):
                            logging.warn("P%d dropping custom endpoint %r. Unreachable even holding "
                                         "every required item.", player, endpoint.name)
                            player_endpoints[player].discard(endpoint)
                            dropped_endpoint = True
                            break
                        _, necessary = get_access_reqs(rule, required_items, endpoint.name)
                        if cls.filter_events:
                            necessary = resolve_event_reqs(necessary, location_necessary, necessary_cache)
                        reqs |= necessary
                    if dropped_endpoint:
                        continue

                    profile_reqs[endpoint] = reqs
                    profile_dropped[endpoint] = {i for i in reqs if not is_path_member(i, 1)}
                    logging.info("P%d custom endpoint %r: %d requirements, %d not nameable (%s)",
                                 player, endpoint.name, len(reqs), len(profile_dropped[endpoint]),
                                 sorted(i.name for i in profile_dropped[endpoint]))

            # Each endpoint has its own path
            for endpoint in player_endpoints[player]:
                # The endpoint is required to win too, even if nothing gates it. Events aren't
                # collectable, so they're not go-mode items.
                if not endpoint.is_event:
                    go_mode_items.setdefault(player, set()).add(endpoint)

                paths[player][endpoint] = {}
                path = paths[player][endpoint]

                # Strongest arrival first. Certainty is a bottleneck cost — a child is
                # max(parent, edge), so it never decreases along an edge — which makes
                # best-first order-independent. A plain stack made the result depend on pop
                # order, and pop order varies per process because Item hashing is seeded.
                visited: Set[Location] = set()
                frontier: List[Tuple[int, int, str, int, Location, Certainty]] = []
                arrival_sequence = itertools.count()

                def reach(location, certainty) -> None:
                    heapq.heappush(frontier, (-CERTAINTY_RANK[certainty], location.player,
                                              location.name, next(arrival_sequence),
                                              location, certainty))

                def walk_requirements(requirements, source, arrival) -> None:
                    """Record each requirement as a path member, then queue the locations holding
                    the copies this route needs."""
                    necessary_here = (set(requirements) if source is None
                                      else resolved_loc_necessary.get(source, set()))

                    for item, count in requirements.items():
                        if cls.filter_other_endpoints and item in player_endpoints[player]:
                            continue

                        # An item keeps the arrival's strength only if it's necessary at the
                        # location that pulled it in. Otherwise this route merely chose it.
                        certainty = arrival
                        if certainty is Certainty.Necessary and item not in necessary_here:
                            certainty = Certainty.Route

                        if is_path_member(item, count):
                            member = path.get(item)
                            if member is None:
                                path[item] = PathMember(item, count, certainty)
                                if is_hintable(item):
                                    items_to_paths.setdefault(item, set()).add(endpoint)
                            else:
                                member.count = max(member.count, count)
                                if CERTAINTY_RANK[certainty] > CERTAINTY_RANK[member.certainty]:
                                    member.certainty = certainty

                        # Expansion is gated, not filtered. Walking every copy of a bulk item
                        # collapses every path to the whole required pool — measured at 146 of
                        # 157 items, identical for all 13 endpoints in the test seed. Walking the
                        # cheapest `count` copies instead stays bounded (+3 to +27 items) and
                        # keeps the subtree behind the gate, which is where the cross-world
                        # structure lives.
                        total = graph_copies[item]
                        child_certainty = (Certainty.BulkCluster if 1 < total and count < total
                                           else certainty)
                        for holder in cheapest_holders(item, count):
                            reach(holder, child_certainty)

                if isinstance(endpoint, CustomEndpoint):
                    # Profile endpoints name items directly rather than sitting at a location.
                    walk_requirements(Counter(dict.fromkeys(profile_reqs[endpoint], 1)),
                                      None, Certainty.Necessary)
                else:
                    walk_requirements(location_req_counts.get(endpoint.location, Counter()),
                                      endpoint.location, Certainty.Necessary)

                while frontier:
                    *_, location, arrival = heapq.heappop(frontier)
                    if location in visited:
                        continue
                    visited.add(location)
                    walk_requirements(location_req_counts.get(location, Counter()),
                                      location, arrival)
                # Nothing gates this endpoint — it's precollected or available from the start.
                # There's no path to hint about, so drop it; it stays in player_endpoints and
                # go_mode_items for hint types that don't need a path.
                if cls.filter_empty_paths and not path:
                    del paths[player][endpoint]

        t6 = time.perf_counter()

        # Why multi-copy requirements never made it onto a path. Also prints each copy's
        # classification: Item collapses on (name, player), so the instance a requirement set
        # holds is an arbitrary copy — and if copies disagree on classification, that's the
        # same arbitrary-copy hazard as item.location, just a different attribute.
        for key, reasons in sorted(refusals.items()):
            if key in admitted or item_counts[key] <= 1:
                continue
            copies = [loc.item for loc in multiworld.get_filled_locations()
                      if (loc.item.name, loc.item.player) == key]
            logging.info("P%d %s never admitted (%d copies, classifications %s): %s",
                         key[1], key[0], item_counts[key],
                         sorted({c.classification.name for c in copies}),
                         "; ".join(sorted(reasons)))

        # Endpoints with no meaningful path: either nothing gates them at all, or the only
        # things that do are items we deliberately suppress. Either way the player isn't
        # blocked, so they get their own hint. The value is the suppressed gates — empty when
        # the endpoint was simply free. A long list here means the thresholds are too tight.
        if cls.filter_empty_paths:
            for player, eps in player_endpoints.items():
                for ep in eps:
                    if ep in paths.get(player, {}):
                        continue
                    if isinstance(ep, CustomEndpoint):
                        pathless_endpoints[player][ep] = profile_dropped.get(ep, set())
                        continue
                    if ep.location is None:
                        continue
                    gates = location_req_counts.get(ep.location, Counter())
                    pathless_endpoints[player][ep] = {item for item, count in gates.items()
                                                      if not is_path_member(item, count)}
        if cls.dump_profile_reference:
            reference = {}
            for player in all_players:
                world = multiworld.worlds[player]
                filled = multiworld.get_filled_locations(player)
                reference[multiworld.player_name[player]] = {
                    "game": multiworld.game[player],
                    "player": player,
                    "events": sorted({loc.item.name for loc in filled if loc.item.code is None}),
                    "item_name_groups": {group: sorted(names)
                                         for group, names in sorted(world.item_name_groups.items())},
                    "location_name_groups": sorted(world.location_name_groups),
                    "precollected": sorted(i.name for i in multiworld.precollected_items[player]),
                    "endpoints": {e.name: len(paths.get(player, {}).get(e, ()))
                                  for e in sorted(player_endpoints[player], key=lambda x: x.name)},
                    "options": {name: str(getattr(world.options, name).value)
                                for name in sorted(world.options_dataclass.type_hints)},
                }
            reference_path = os.path.join(run_dir, "profile_reference.json")
            with open(reference_path, "w", encoding="utf-8") as f:
                json.dump(reference, f, indent=2)
            logging.info("Wrote %s", reference_path)

        if cls.build_item_requirements:
            def item_key(item: Item) -> str:
                return f"{item.name} (P{item.player})"

            requirements_dump = {
                "locations": {
                    f"{location.name} (P{location.player})": {
                        "item": item_key(location_contents[location]),
                        "world": location.player,
                        "requires": {item_key(req): count for req, count in requirements.items()},
                    }
                    for location, requirements in sorted(location_req_counts.items(),
                                                         key=lambda kv: (kv[0].player, kv[0].name))
                },
                "starting_items": {str(player): dict(items)
                                   for player, items in starting_items.items()},
            }

            requirements_path = os.path.join(run_dir, "item_requirements.json")
            with open(requirements_path, "w", encoding="utf-8") as f:
                json.dump(requirements_dump, f, indent=2)
            logging.info("Wrote %s (%d bytes)", requirements_path,
                         os.path.getsize(requirements_path))

        if cls.dump_paths:
            dump = {}
            for player, endpoints in paths.items():
                dump[str(player)] = {
                    "game": multiworld.game[player],
                    "name": multiworld.player_name[player],
                    "path_sizes": {endpoint.name: len(path)
                                   for endpoint, path in sorted(endpoints.items(), key=lambda kv: kv[0].name)},
                    "paths": {
                        endpoint.name: [
                            {
                                "name": member.item.name,
                                "player": member.item.player,
                                "is_event": member.item.is_event,
                                "count": member.count,
                                "certainty": member.certainty,
                                "location": member.item.location.name if member.item.location else None,
                                "location_player": member.item.location.player if member.item.location else None,
                            }
                            for member in sorted(path.values(), key=lambda m: (m.item.player, m.item.name))
                        ]
                        for endpoint, path in sorted(endpoints.items(), key=lambda kv: kv[0].name)
                    },
                }

            # Stamp the settings that produced this file — two dumps from different flag states are
            # otherwise indistinguishable. Underscore prefixes can't collide with the player ids.
            dump["_flags"] = {name: getattr(cls, name) for name in
                              ("filter_events", "filter_empty_paths", "filter_other_endpoints",
                               "expand_single_endpoints", "filter_multi_copy_items",
                               "filter_forced_early", "filter_locks_too_much")}
            dump["_thresholds"] = {"bulk_item_threshold": cls.bulk_item_threshold,
                                   "locks_too_much_threshold": cls.locks_too_much_threshold}
            dump["_pathless_endpoints"] = {
                str(p): {ep.name: sorted(g.name for g in gates) for ep, gates in eps.items()}
                for p, eps in sorted(pathless_endpoints.items())}

            dump_path = os.path.join(run_dir, "paths.json")

            with open(dump_path, "w", encoding="utf-8") as f:
                json.dump(dump, f, indent=2, sort_keys=False)
            logging.info("Wrote %s (%d players, %d paths)", dump_path, len(dump) - 3,
                         sum(len(p["paths"]) for k, p in dump.items() if not k.startswith("_")))

        logging.info("ArchiLogic timings: build-spheres %.2fs  prune-spheres %.2fs  rebuild-spheres %.2fs  "
                     "required-locs %.2fs  access-reqs %.2fs  resolve-events %.2fs  filters %.2fs  "
                     "endpoints+paths %.2fs  total %.2fs",
                     t1a - t0, t1b - t1a, t1 - t1b, t2 - t1, t3 - t2, t4 - t3, t5 - t4, t6 - t5, t6 - t0)

        player_regions = {p: {r.name for r in multiworld.get_regions(p)}
                          for p in all_players}

        def player_has_regions(player: int) -> bool:
            return True if player_regions[player] and len(player_regions[player]) > 1 else False

        t7 = time.perf_counter()

        # Step Six: Create all the hints for all the hintable items.
        @dataclass
        class HintSet:
            generic_woth: Optional[WothHint] = None
            specific_woth: Optional[WothHint] = None
            single_paths: Set[SinglePathHint] = field(default_factory=set)
            multi_path: Optional[MultiPathHint] = None

        hint_sets: Dict[Item, HintSet] = {}
        for item, endpoints in items_to_paths.items():
            if not (
                    cls.create_single_paths or
                    cls.create_multi_paths or
                    cls.create_specific_woths or
                    cls.create_generic_woths
            ):
                logging.debug("Hint creation disabled. For some reason.")
                break

            # Determine how directly we hint the location/item. Can be overridden by profiles.
            if item.location is None:
                hint_directness = Directness.Item
            elif profile_for(multiworld.game[item.location.player]).prefer_loc_over_region:
                hint_directness = Directness.Location
            else:
                hint_directness = Directness.Region

            hint_set = HintSet()
            path_players = set()
            endpoints_dict: Dict[Tuple[int, str], Set[Item]] = defaultdict(set)

            for end in endpoints:
                if not (cls.create_single_paths or cls.create_multi_paths or cls.create_specific_woths):
                    break

                if cls.create_single_paths:
                    # Create single-path hint
                    hint_set.single_paths.add(
                        SinglePathHint(
                            item,
                            end,
                            hint_directness,
                        )
                    )

                if cls.create_multi_paths:
                    # Add to dict for multi-path hint
                    endpoints_dict[(end.player, multiworld.player_name[end.player])].add(end)

                if cls.create_specific_woths:
                    # Keep track of which players worlds this is path to
                    path_players.add(end.player)

            # Add multipath hint for item
            if cls.create_multi_paths:
                hint_set.multi_path = MultiPathHint(
                    item,
                    endpoints_dict,
                    hint_directness,
                )

            # Add all WOTH hints for item
            if cls.create_generic_woths:
                hint_set.generic_woth = WothHint(
                    item,
                    hint_directness,
                    {},
                    False
                )

            if cls.create_specific_woths:
                hint_set.specific_woth = WothHint(
                    item,
                    Directness.Location if hint_directness == Directness.Region else hint_directness,
                    {slot_id: multiworld.player_name[slot_id] for slot_id in path_players},
                    len(path_players) == len(all_players),
                )

            hint_sets[item] = hint_set

        t8 = time.perf_counter()

        # Now select randomly select which hints go into the pool based on the seed
        hint_candidates: Dict[int, List[Item]] = defaultdict(list)
        for item in hint_sets:
            hint_candidates[item.player].append(item)
        for items in hint_candidates.values():
            items.sort(key=lambda i: (i.player, i.name))

        selected_hints = []
        for player in all_players:
            selected_items = archilogic_world.random.sample(
                hint_candidates[player], min(len(hint_candidates[player]), cls.hints_per_player))

            # For now I'm just going 2/3rds of hints are multipath, remaining are specific woth
            multipath_num = math.ceil(len(selected_items) * 2 / 3)
            woth_num = len(selected_items) - multipath_num

            player_hints = []
            for item in selected_items:
                hint_set = hint_sets[item]
                if multipath_num > 0 and hint_set.multi_path:
                    player_hints.append(hint_set.multi_path)
                    multipath_num -= 1
                elif woth_num > 0 and hint_set.specific_woth:
                    player_hints.append(hint_set.specific_woth)
                    woth_num -= 1

            # Do a seeded random shuffle so the hints aren't ordered by type
            archilogic_world.random.shuffle(player_hints)
            selected_hints.extend(player_hints)

        for index, hint in enumerate(selected_hints):
            hint.id = index

        archilogic_world.selected_hints = selected_hints
        # Only locations the server knows about — event locations have no address and
        # never arrive as checks, so counting them would inflate the denominator the
        # grant and hint_toward percentages are measured against.
        archilogic_world.location_counts = {
            str(player): sum(1 for loc in multiworld.get_locations(player)
                             if isinstance(loc.address, int))
            for player in all_players
        }

        if cls.build_item_requirements:
            # Display names are the only names the client ever sees, so two distinct
            # items resolving to the same one would merge into a single graph node and
            # let a requirement for one be satisfied by the other. Check the shipped
            # set, not the profile's dict — get_display_name also falls through to the
            # apworld's hint_text, which no profile author sees.
            shipped: Dict[Tuple[int, str], Set[str]] = defaultdict(set)
            for item in all_available_items:
                shipped[(item.player, get_display_name(item))].add(item.name)
            for (player, shown), raw_names in sorted(shipped.items()):
                if len(raw_names) > 1:
                    raise ArchiLogicError(
                        f"P{player}: display name {shown!r} maps to several distinct items: "
                        f"{sorted(raw_names)}. Display names must be unique per player.")

            archilogic_world.location_graph = [
                {
                    "name": location.name,
                    "world": location.player,
                    "sphere": location_sphere_depth[location],
                    "address": location.address if isinstance(location.address, int) else None,
                    "item": [get_display_name(location_contents[location]),
                             location_contents[location].player],
                    "requires": [[get_display_name(requirement), requirement.player, count]
                                 for requirement, count in sorted(
                                     requirements.items(), key=lambda kv: (kv[0].player, kv[0].name))],
                }
                for location, requirements in sorted(location_req_counts.items(),
                                                     key=lambda kv: (kv[0].player, kv[0].name))
            ]

            # CustomEndpoints are synthetic — no location, so nothing in the loop above
            # emits them. Without a node, /hint_toward can't walk to one even though
            # hints name it. address=None marks it unobservable, which is correct: the
            # client collects it once its requirements are met, same as an event.
            archilogic_world.location_graph.extend(
                {
                    "name": endpoint.name,
                    "world": endpoint.player,
                    "sphere": len(collection_spheres),
                    "address": None,
                    "item": [endpoint.name, endpoint.player],
                    "requires": [[get_display_name(req), req.player, 1]
                                 for req in sorted(reqs, key=lambda i: (i.player, i.name))],
                }
                for endpoint, reqs in sorted(profile_reqs.items(), key=lambda kv: (kv[0].player, kv[0].name))
            )

            archilogic_world.starting_items = {str(player): dict(items)
                                               for player, items in starting_items.items()}

            logging.info("slot_data payload: %d bytes",
                         len(json.dumps(convert_to_base_types(archilogic_world.fill_slot_data()))))

        logging.info("Hint-Building timing =  %.2fs",t8 - t7)
        logging.info(f"Hints built: "
                     f"{'\n\t- Generic Woth Hints' if cls.create_generic_woths else ''}"
                     f"{'\n\t- Specific Woth Hints' if cls.create_specific_woths else ''}"
                     f"{'\n\t- Single Path Hints' if cls.create_single_paths else ''}"
                     f"{'\n\t- Multi Path Hints' if cls.create_multi_paths else ''}")

        logging.info("either/or locations: %d total, %d on paths",
                     len(either_or_locs),
                     len({i.location for p in paths.values() for path in p.values()
                          for i in path if i.location in either_or_locs}))

        logging.getLogger().removeHandler(run_log)
        run_log.close()
