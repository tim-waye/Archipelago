import json
import logging
import time
from typing import Dict, List, Optional, Set, Tuple
from collections import Counter, defaultdict

from BaseClasses import CollectionState, Location, Item, ItemClassification, DEFAULT_COLLECTION_RULE, Region
from worlds.AutoWorld import World

class ArchiLogicWorld(World):
    game = "ArchiLogic"

    # TODO make these overrideable from options in the yaml. Per game preferably
    bulk_item_threshold = 6
    hintable_item_classes = [
        ItemClassification.progression,
        ItemClassification.progression_skip_balancing,
        ItemClassification.progression_deprioritized_skip_balancing,
    ]

    item_name_to_id: Dict[str, int] = {}
    location_name_to_id: Dict[str, int] = {}

    @classmethod
    def stage_pre_output(cls, multiworld):
        logging.info("Entered Stage Pre-Output for ArchiLogic.")
        t0 = time.perf_counter()

        # Step One: In order to calculate the critical paths for the multiworld and/or each player, we essentially need our
        # own version of the playthrough from the spoiler log. So we're going to copy how the Spoiler class does that.
        logging.info("Building Playthrough to calculate critical paths.")
        prog_locations = {location for location in multiworld.get_filled_locations() if location.item.advancement}
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
                else:
                    # Unreachable locations might be useful for foolish hints in the future
                    unreachable_locations = sphere_candidates
                    break

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

        # Stage Three: Check if precollected items are required
        required_starting_items = set()
        for precollected_items in multiworld.precollected_items.values():
            for item in precollected_items.copy():
                if not item.advancement:
                    continue
                logging.debug('Checking if %s (Player %d) is required to beat the game.', item.name, item.player)
                test_state = multiworld.state.copy()
                test_state.remove(item)
                if not multiworld.can_beat_game(test_state, required_locations):
                    required_starting_items.add(item)

        # Stage Four: Repair any unintentional links that may have been made in step two.
        # One last forward collection iteration through the state.
        required_locations = {item for sphere in collection_spheres for item in sphere}
        state = CollectionState(multiworld)
        collection_spheres = []
        while required_locations:
            sphere = set(filter(state.can_reach, required_locations))

            for loc in sphere:
                state.collect(loc.item, True, loc)

            collection_spheres.append(sphere)

            for player in multiworld.player_ids:
                if multiworld.game[player] == cls.game: continue
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
        for player in multiworld.player_ids:
            if multiworld.game[player] == cls.game: continue
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
        item_counts = Counter((loc.item.name, loc.item.player) for loc in multiworld.get_filled_locations())
        item_counts += Counter((i.name, i.player) for items in multiworld.precollected_items.values()
                               for i in items if i.advancement)
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
        for player in multiworld.player_ids:
            if multiworld.game[player] == cls.game: continue
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

        # Step Five: Determine which items and location are worth giving hints for.
        # We're actually going to do double duty here and also compile a list of bulk items and their regions for use
        # in other potential hints.
        def is_hintable(item):
            return (not item.is_event
                    and (item.classification in cls.hintable_item_classes)
                    and item.name not in multiworld.worlds[item.player].hint_blacklist)

        hintable_loc_reqs: Dict[Location, Set[Item]] = defaultdict(set)
        hintable_copies: Dict[Item, Set[Optional[Location]]] = defaultdict(set)
        required_bulk: Set[Tuple[str, int]] = set()
        for loc, items in resolved_loc_necessary.items():
            for item in items:
                if not is_hintable(item):
                    continue
                if item_counts[(item.name, item.player)] > cls.bulk_item_threshold:
                    # Required bulk items may earn a region hint, tracked separately below.
                    required_bulk.add((item.name, item.player))
                    continue
                if is_multi_copy(item):
                    # For now, treat copies of one item as an either/or with each other.
                    continue
                hintable_loc_reqs[loc].add(item)
                hintable_copies[item].add(item.location)

        # Which regions hold path-relevant items, for "this region has 3 things you need" hints.
        hintable_items_by_region: Dict[Region, Counter[Item]] = defaultdict(Counter)
        for item, copies in hintable_copies.items():
            for copy in copies:
                if copy is not None:        # None is a precollected copy — no region to name
                    hintable_items_by_region[copy.parent_region][item] += 1

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
        def stored_name(player, item):
            state = CollectionState(multiworld)
            before = Counter(state.prog_items[player])
            state.collect(item, True)
            after = state.prog_items[player]
            changed = [k for k in after if after[k] > before.get(k, 0)]
            return changed[0] if len(changed) == 1 else None

        def goal_deps(player, final_state):
            seeds = set()
            for item in goal_items(player, final_state):
                if item.is_event and item.location is not None:
                    seeds.update(resolved_loc_reqs[item.location])
                else:
                    seeds.add(item)
            return seeds

        def goal_items(player, final_state):
            items_for_player = items_by_name(player)  # name -> Item
            found = set()
            for name, item in items_for_player.items():
                # Make sure we're using the right item name
                key = name if name in final_state.prog_items[player] else stored_name(player, item)

                # If it's not a prog item it can't be a goal item
                count = final_state.prog_items[player].get(key, 0) if key else 0
                if not count:
                    continue

                test_state = final_state.copy()
                test_state.remove_item(key, player, count)
                test_state.reachable_regions[player] = set()
                test_state.blocked_connections[player] = set()
                test_state.stale[player] = True
                if not multiworld.completion_condition[player](test_state):
                    found.add(item)
            return found

        def goal_locations(player, final_state):
            found = set()
            for loc in multiworld.get_locations(player):
                original = loc.access_rule
                loc.access_rule = lambda state: False
                try:
                    if not multiworld.completion_condition[player](final_state):
                        found.add(loc)
                finally:
                    loc.access_rule = original
            return found

        def endpoint_items(player, final_state, deps):
            """
            Finds which items directly lock and/or represent the win condition for this player's game.
            Three ways an item qualifies, checked in order:
              1. it's read by a goal location's own access rule
              2. the goal fails without it while every goal location stays reachable, meaning the goal predicate
                 reads it directly
              3. it guards the nearest guarded entrance into a goal location's region
            """
            goal_locs = goal_locations(player, final_state)

            # two probes per dep:
            #   deep    - caches cleared, so reachability is recomputed (test 2)
            #   shallow - caches intact, so only rules reading prog_items directly flip.
            #             This is what separates `state.has(X)` gates from `can_reach(...)`
            #             gates, which otherwise flip for every item on the path.
            probes = {}
            probes_shallow = {}
            for dep in deps:
                key = (dep.name if dep.name in final_state.prog_items[player]
                       else stored_name(player, dep))
                count = final_state.prog_items[player].get(key, 0) if key else 0
                if not count:
                    logging.warning("ArchiLogic: %s (P%d, %s) has no prog_items entry; "
                                    "excluded from endpoints.",
                                    dep.name, player, multiworld.game[player])
                    continue

                probe = final_state.copy()
                probe.remove_item(key, player, count)
                probe.reachable_regions[player] = set()
                probe.blocked_connections[player] = set()
                probe.stale[player] = True
                probes[dep] = probe

                shallow = final_state.copy()
                shallow.remove_item(key, player, count)
                probes_shallow[dep] = shallow

            deps = {d for d in deps if d in probes}

            access_for_spot = {}
            def flips(spot, dep):
                if spot not in access_for_spot:
                    access_for_spot[spot] = spot.access_rule(final_state)
                return access_for_spot[spot] and not spot.access_rule(probes_shallow[dep])

            found = set()
            remaining = set()

            for dep in deps:
                # 1. breaks a goal location's own rule
                if any(flips(gl, dep) for gl in goal_locs):
                    found.add(dep)
                # 2. goal reads it directly (glocs empty => goal has no location component)
                elif not goal_locs or all(gl.can_reach(probes[dep]) for gl in goal_locs):
                    found.add(dep)
                else:
                    remaining.add(dep)

            logging.info("P%d tests 1+2 caught: %s", player, sorted(i.name for i in found))
            logging.info("P%d remaining:       %s", player, sorted(i.name for i in remaining))

            # 3. guards the nearest item-named gate into a goal region.
            #    Walk back only through genuinely free connections; an entrance with a
            #    rule that no item flips (e.g. `_can_get(...)`) is still a closed door —
            #    it just names no item, so there's no endpoint to report there.
            free_state = baseline

            for gl in goal_locs:
                seen, frontier = set(), [gl.parent_region]
                while frontier:
                    region = frontier.pop()
                    if region is None or region in seen:
                        continue
                    seen.add(region)
                    for entrance in region.entrances:
                        guards = {d for d in remaining if flips(entrance, d)}
                        if guards:
                            found |= guards
                        elif entrance.access_rule(free_state):
                            frontier.append(entrance.parent_region)

            logging.info("P%d after test 3:    %s", player, sorted(i.name for i in found))

            if not found or found == deps:
                found = set(deps)

            # 4. One last edge case.
            # If a game only has one single endpoint then paths are no different from standard WOTH hints.
            # In this case we'll drill down until we find multiple requirements that unlock the goal condition.
            #
            # Read the UNRESOLVED requirements here. Step four swaps events out for the items behind them since hinting
            # items makes more sense than hinting events along the way. BUT for an endpoint, events will make sense.
            # If your goal condition is "beat the end boss" then my hope is, if it's not represented by getting some
            # item, or checking some location, then HOPEFULLY it would be a "beat the end boss" event.
            # (I guess we'll see how that works out. But it sounds good for now)
            seen = set()
            while len(found) == 1:
                item = next(iter(found))
                if item in seen or item.location is None:
                    break
                seen.add(item)
                deeper = {i for i in location_necessary.get(item.location, ())
                          if i.is_event or is_hintable(i)}
                deeper.discard(item)
                if not deeper:
                    break
                found = deeper

            logging.info("P%d endpoints:       %s", player, sorted(i.name for i in found))
            return found

        # Oh boy, finally time to build our paths!
        goal_dependencies = {}
        player_endpoints = {}
        go_mode_items: Dict[int, Set[Item]] = {}

        # Each player has one path per endpoint. The path is the set of all hintable items across the multiworld that
        # are required to unlock the endpoint.
        paths: Dict[int, Dict[Item, Set[Item]]] = {}

        # Keep track of which paths each item is on. This will be used to give path/multi-path hints.
        # The hints will be structured in some form of: "This item is on the path to these endpoints"
        items_to_paths = {}

        for player in multiworld.player_ids:
            if multiworld.game[player] == cls.game: continue
            paths[player] = {}
            goal_dependencies[player] = goal_deps(player, state)
            player_endpoints[player] = endpoint_items(player, state, goal_dependencies[player])

            # Each endpoint has its own path
            for endpoint in player_endpoints[player]:
                paths[player][endpoint] = set()
                path = paths[player][endpoint]
                endpoint_reqs = hintable_loc_reqs[endpoint.location]

                req_stack = list(endpoint_reqs)
                while req_stack:
                    item = req_stack.pop()
                    if item in path:
                        continue
                    if item in player_endpoints[player]:
                        continue
                    if is_multi_copy(item):
                        # Several copies exist, but our code (for now) assumes that all multi-copy items are either/ors.
                        # Not technically true, but I can't think of a way to handle it correctly without quintupling gen time lmao
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

        t4 = time.perf_counter()
        logging.info("ArchiLogic timings: step1 %.2fs  step2 %.2fs  step3 %.2fs  step4 %.2fs  total %.2fs",
                     t1 - t0, t2 - t1, t3 - t2, t4 - t3, t4 - t0)
        logging.info("Completed calculating backing structures for hints.")
        #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        # Build up the logical backing for our hints:
        # WOTH and Path hints -> Determine critical path and each location on the path's requirements, and what's "hintable"
        # Region hints -> Determine amount of woth items in each region, give hints on woth and/or foolish regions
        # Who has my item? -> Maintain player links for locations. Like built in hints but less direct.
        #                  -> Can also hint region for item to point players in the right direction without "spoiling"
        #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
