import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Any
from collections import Counter, defaultdict

import math

from BaseClasses import CollectionState, Location, Item, ItemClassification, DEFAULT_COLLECTION_RULE, Region
from Options import OptionError
from worlds.AutoWorld import World
from worlds.archilogic.HintClasses import MultiPathHint, Directness, SinglePathHint, WothHint, Hint


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
    hintable_item_classes = [
        ItemClassification.progression,
        ItemClassification.progression_skip_balancing,
        ItemClassification.progression_deprioritized_skip_balancing,
    ]

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

    hints_per_player = 20
    selected_hints: List[Hint] = []

    def fill_slot_data(self) -> Dict[str, Any]:
        return {"hints": [h.to_slot_data() for h in self.selected_hints]}

    @classmethod
    def stage_pre_output(cls, multiworld):
        logging.info("Entered Stage Pre-Output for ArchiLogic.")
        t0 = time.perf_counter()

        all_players = [player for player in multiworld.player_ids if multiworld.game[player] != cls.game]
        archilogic_world = multiworld.worlds[multiworld.get_game_players(cls.game)[0]]

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

        # Also need to handle the edge case of items that have multiple copies with the same name.
        # How many copies of each item exist anywhere in the multiworld, starting inventory included.
        # item_counts = Counter((loc.item.name, loc.item.player) for loc in multiworld.get_filled_locations())
        # item_counts += Counter((i.name, i.player) for items in multiworld.precollected_items.values()
        #                        for i in items if i.advancement)
        def is_multi_copy(item):
            """This item has multiple copies, but not enough to be treated as a bulk item"""
            return 1 < item_counts[(item.name, item.player)] <= cls.bulk_item_threshold

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
        # Due to the algorithm I'm using, if an access_reqs set does contain an either/or, it will only have the last
        # valid set that it tested.
        # But, the whole reason to call out these either/ors is because they make hints confusing, so our solution will
        # be to simply not give hints for items only used in either/or situations. So it's unnecessary (at this point)
        # to calculate every potential set of optional items for each either/or, since ultimately we're going to trim
        # them out anyway.
        def get_access_reqs(target: Location | Region, candidates: List[Item]) -> Tuple[Set[Item], Set[Item]]:
            access_reqs: Set[Item] = set()
            min_reqs: Set[Item] = set()
            counts = Counter(candidates)

            if not target.can_reach(baseline):
                # Make a state object with all our candidates collected so we can test if each one is needed
                state = baseline.copy()
                for item in candidates:
                    state.collect(item, True)
                assert target.can_reach(state), f"{target} unreachable with every candidate held"
                access_reqs = set(counts)

                # Remove all of each item from the state and test if we can still reach the target.
                # If so we discard the item(s) from our needed set, because they aren't needed.
                # If removing them means we can't reach the target anymore we re-collect them and keep them in the needed set
                for item in sorted(counts):
                    n = counts[item]
                    for _ in range(n):
                        state.remove(item)
                    if target.can_reach(state):
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
                if not target.can_reach(state):
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
                    region_reqs[region],  region_necessary[region] = get_access_reqs(region, candidates)

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
                        location_reqs[loc], location_necessary[loc] = get_access_reqs(loc, candidates)

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

        # Now we can most efficiently calculate the locs/regions that have either/or requirements,
        # as well as determine which sets of items are used in these either/ors.
        #
        # Because of the nature of either/ors, we can't definitively say one of those combinations is required over any
        # other. We also can't say all of them are required because that's untrue and would be a misleading hint.
        # So the policy is to strip either/or items from paths entirely.
        #
        # Instead we'll try to give more priority to hinting the LOCATION that has the either/or requirements gating it.
        # A player who knows their game well should know which locations have multiple ways to access them. And being
        # told they'll have to figure out a way in is more interesting and useful than being handed one arbitrary route.
        #
        # So for our temple example, say that locked chest holds a magical sword which is required to beat the end boss.
        # We could potentially have a hint saying "The Chest Key is on the path to your Magical Sword", so players will
        # know they need to enter the temple. But they would NOT receive hints saying the Bombs, Gem, or Orb are on the
        # path to the Magical Sword. (Unless of course those items ALSO gate some other required item on the path!)
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

        # Step Five: Determine which items and location are worth giving hints for.

        def is_forced_early(item):
            if not cls.filter_forced_early:
                return False
            return (item.name in multiworld.early_items.get(item.player, {})
                    or item.name in multiworld.local_early_items.get(item.player, {}))

        def is_hintable(item):
            return ((not cls.filter_events or not item.is_event)
                    and (item.classification in cls.hintable_item_classes)
                    and item.name not in multiworld.worlds[item.player].hint_blacklist
                    and not is_forced_early(item))

        hintable_loc_reqs: Dict[Location, Set[Item]] = defaultdict(set)
        required_bulk: Set[Tuple[str, int]] = set()

        necessary_reqs = resolved_loc_necessary if cls.filter_events else location_necessary
        for loc, items in necessary_reqs.items():
            for item in items:
                if not is_hintable(item):
                    continue
                if item_counts[(item.name, item.player)] > cls.bulk_item_threshold:
                    # Required bulk items may earn a region hint, tracked separately below.
                    required_bulk.add((item.name, item.player))
                    continue
                if cls.filter_multi_copy_items and is_multi_copy(item):
                    # For now, treat copies of one item as an either/or with each other.
                    continue
                hintable_loc_reqs[loc].add(item)

        # Some games have single items that lock off a large percentage of their checks. This makes the item so
        # ubiquitous that in most seeds it would end up on any path that has any of its items in that game. Thus making
        # it not a very useful item to hint.
        # So if any item locks more than our set threshold we'll make it ineligible for path hinting.
        everything = CollectionState(multiworld)
        for loc in multiworld.get_filled_locations():
            everything.collect(loc.item, True)

        candidates_by_player: Dict[int, Set[Item]] = defaultdict(set)
        for reqs in hintable_loc_reqs.values():
            for item in reqs:
                candidates_by_player[item.player].add(item)

        locks_too_much: Set[Item] = set()
        if cls.filter_locks_too_much:
            for gate_player, candidates in candidates_by_player.items():
                reachable = [loc for loc in multiworld.get_locations(gate_player)
                             if loc.can_reach(everything)]
                if not reachable:
                    continue
                for item in candidates:
                    probe = everything.copy()
                    probe.remove(item)
                    locked = sum(1 for loc in reachable if not loc.can_reach(probe))
                    if 100 * locked / len(reachable) >= cls.locks_too_much_threshold:
                        locks_too_much.add(item)

        logging.debug("Excluded because it locks too much of someone's game: %s",
                     sorted(f"{i.name} (P{i.player})" for i in locks_too_much))

        for reqs in hintable_loc_reqs.values():
            reqs -= locks_too_much
        t5 = time.perf_counter()

        # Locate which regions hold each copy of required bulk items.
        # Potentially useful for a bulk item hint like: "Need more Power Stars? Player X's Y region has 4 of them!"
        bulk_by_region: Dict[Item, Counter[Region]] = defaultdict(Counter)
        for loc in multiworld.get_filled_locations():
            if (loc.item.name, loc.item.player) in required_bulk:
                bulk_by_region[loc.item][loc.parent_region] += 1

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
                             if i not in locks_too_much and not is_forced_early(i)}
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
                                or (is_hintable(req)
                                    and copies <= cls.bulk_item_threshold
                                    and not (cls.filter_multi_copy_items and copies > 1)
                                    and req not in locks_too_much)):
                            deeper.add(req)
                    deeper.discard(item)
                    if not deeper:
                        break
                    found_endpoints = deeper

            logging.info("P%d endpoints:       %s", player, sorted(i.name for i in found_endpoints))
            return found_endpoints

        # Oh boy, finally time to build our paths!
        player_endpoints = {}
        go_mode_items: Dict[int, Set[Item]] = {}

        # Each player has one path per endpoint. The path is the set of all hintable items across the multiworld that
        # are required to unlock the endpoint.
        paths: Dict[int, Dict[Item, Set[Item]]] = {}

        # Keep track of which paths each item is on. This will be used to give path/multi-path hints.
        # The hints will be structured in some form of: "This item is on the path to these endpoints"
        items_to_paths = {}

        # Keep track of any endpoints that don't end up with useful paths to hint
        pathless_endpoints: Dict[int, Dict[Item, Set[Item]]] = defaultdict(dict)

        for player in all_players:
            paths[player] = {}
            player_endpoints[player] = endpoint_items(player, state)

            # Each endpoint has its own path
            for endpoint in player_endpoints[player]:
                # The endpoint is required to win too, even if nothing gates it. Events aren't
                # collectable, so they're not go-mode items.
                if not endpoint.is_event:
                    go_mode_items.setdefault(player, set()).add(endpoint)

                paths[player][endpoint] = set()
                path = paths[player][endpoint]
                endpoint_reqs = hintable_loc_reqs[endpoint.location]

                req_stack = list(endpoint_reqs)
                while req_stack:
                    item = req_stack.pop()
                    if item in path:
                        continue
                    if cls.filter_other_endpoints and item in player_endpoints[player]:
                        continue

                    # Item is on the path
                    path.add(item)
                    if not items_to_paths.get(item):
                        items_to_paths[item] = set()
                    items_to_paths[item].add(endpoint)
                    # Which means it's also required for go-mode
                    if not go_mode_items.get(player):
                        go_mode_items[player] = set()
                    go_mode_items[player].add(item)

                    # If it has further requirements, those should be on the path too
                    if not item.location is None:
                        req_stack.extend(hintable_loc_reqs[item.location])

                # Nothing gates this endpoint — it's precollected or available from the start.
                # There's no path to hint about, so drop it; it stays in player_endpoints and
                # go_mode_items for hint types that don't need a path.
                if cls.filter_empty_paths and not path:
                    del paths[player][endpoint]

        # Endpoints with no meaningful path: either nothing gates them at all, or the only
        # things that do are items we deliberately suppress. Either way the player isn't
        # blocked, so they get their own hint. The value is the suppressed gates — empty when
        # the endpoint was simply free. A long list here means the thresholds are too tight.
        if cls.filter_empty_paths:
            for player, eps in player_endpoints.items():
                for ep in eps:
                    if ep in paths.get(player, {}) or ep.location is None:
                        continue
                    raw = necessary_reqs.get(ep.location, set())
                    pathless_endpoints[player][ep] = raw - hintable_loc_reqs.get(ep.location, set())
        t6 = time.perf_counter()

        # TEMPORARY — dump paths to JSON for offline comparison.
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
                            "name": i.name,
                            "player": i.player,
                            "is_event": i.is_event,
                            "location": i.location.name if i.location else None,
                            "location_player": i.location.player if i.location else None,
                        }
                        for i in sorted(path, key=lambda x: (x.player, x.name))
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

            hint_set = HintSet()
            hint_directness = Directness.Location if item.location else Directness.Item
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
                    set(),
                    set(),
                    False
                )

            if cls.create_specific_woths:
                hint_set.specific_woth = WothHint(
                    item,
                    hint_directness,
                    path_players,
                    set(multiworld.player_name[player] for player in path_players),
                    path_players == all_players,
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

            for item in selected_items:
                hint_set = hint_sets[item]
                if multipath_num > 0 and hint_set.multi_path:
                    selected_hints.append(hint_set.multi_path)
                    multipath_num -= 1
                elif woth_num > 0 and hint_set.specific_woth:
                    selected_hints.append(hint_set.specific_woth)
                    woth_num -= 1
        archilogic_world.selected_hints = selected_hints


        logging.info("Hint-Building timing =  %.2fs",t8 - t7)
        logging.info(f"Hints built: "
                     f"{'\n\t- Generic Woth Hints' if cls.create_generic_woths else ''}"
                     f"{'\n\t- Specific Woth Hints' if cls.create_specific_woths else ''}"
                     f"{'\n\t- Single Path Hints' if cls.create_single_paths else ''}"
                     f"{'\n\t- Multi Path Hints' if cls.create_multi_paths else ''}")

        logging.getLogger().removeHandler(run_log)
        run_log.close()

        #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        # Build up the logical backing for our hints:
        # WOTH and Path hints -> Determine critical path and each location on the path's requirements, and what's "hintable"
        # Region hints -> Determine amount of woth items in each region, give hints on woth and/or foolish regions
        # Who has my item? -> Maintain player links for locations. Like built in hints but less direct.
        #                  -> Can also hint region for item to point players in the right direction without "spoiling"
        #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
