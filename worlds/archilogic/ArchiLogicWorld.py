import json
import logging
import time
from typing import Dict, List, Optional, Set
from collections import Counter

from BaseClasses import CollectionState, Location, Item, ItemClassification, DEFAULT_COLLECTION_RULE, Region
from worlds.AutoWorld import World

class ArchiLogicWorld(World):
    game = "ArchiLogic"
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
        unreachable_locations: Set[Location] = set()

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
        player_req_locs: Dict[int, Set[Location]] = {}

        # Function to determine whether a location is required for a specific player to beat their game
        def can_player_beat(multiworld, player, locations):
            state = CollectionState(multiworld)
            if multiworld.completion_condition[player](state):
                return True
            for _ in state.sweep_for_advancements(locations, yield_each_sweep=True,
                                                  checked_locations=state.locations_checked):
                if multiworld.completion_condition[player](state):
                    return True
            return False

        # Loop through all required locations to determine which are required for each player.
        global_req_locs = {loc for sphere in collection_spheres for loc in sphere}
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
        region_reqs: Dict[Region, Set[Item]] = {}
        region_necessary: Dict[Region, Set[Item]] = {}
        region_with_either_ors: Dict[Region, Set[Item]] = {}
        location_reqs: Dict[Location, Set[Item]] = {}
        location_necessary: Dict[Location, Set[Item]] = {}
        location_with_either_ors: Dict[Region, Set[Item]] = {}

        # Precollected items can be required, so start with a state that has no advancement items collected.
        baseline = CollectionState(multiworld)
        for items in multiworld.precollected_items.values():
            for item in items:
                if item.advancement: baseline.remove(item)

        # Returns a minimal set of candidate items needed to reach the target.
        # See necessary_subset for why this is A minimal set, not THE minimal set.
        def prune(target, candidates):
            if target.can_reach(baseline):
                return set()
            # Need to use a Counter instead of a set to handle multiple items with the same name
            counts = Counter(candidates)
            s = baseline.copy()
            for item in candidates:
                s.collect(item, True)
            assert target.can_reach(s), f"{target} unreachable with every candidate held"
            needed = set(counts)
            for item in sorted(counts):
                n = counts[item]
                for _ in range(n):
                    s.remove(item)
                if target.can_reach(s):
                    needed.discard(item)        # not needed — leave it out
                else:
                    for _ in range(n):
                        s.collect(item, True)       # required — put it back
            return needed

        # Some locations may have multiple different combinations of items that can make it accessible.
        # We'll call these different options either/ors since you either need one set or another while prune() keeps
        # whichever set of items happens to be tested last.
        # This function will determine if the set returned by prune() contains either/ors, and returns a set without them.
        # This will then return of only items that are known to be necessary for access (at least logically).
        # TODO: This is where I'd like to implement deeper either/or handling in the future
        def necessary_subset(target, candidates, needed):
            if not needed:
                return set()
            # Need to use a Counter instead of a set to handle multiple items with the same name
            counts = Counter(candidates)
            s = baseline.copy()
            for item in candidates:
                s.collect(item, True)
            out = set()
            for item in needed:
                n = counts[item]
                for _ in range(n):
                    s.remove(item)
                if not target.can_reach(s):
                    out.add(item)
                for _ in range(n):
                    s.collect(item, True)           # always restore — each test is against the full set
            return out

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

                    reqs = prune(region, candidates)
                    region_reqs[region] = reqs
                    region_necessary[region] = necessary_subset(region, candidates, reqs)
                    if not reqs == region_necessary[region]:
                        logging.debug("Region %s has either/ors in its requirements", region.name)
                        region_with_either_ors[region] = region_reqs[region]

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
                        reqs = prune(loc, candidates)
                        location_reqs[loc] = reqs
                        location_necessary[loc] = necessary_subset(loc, candidates, reqs)
                        if not reqs == location_necessary[loc]:
                            logging.debug("Location %s has either/ors in its requirements", loc.name)
                            location_with_either_ors[loc] = location_reqs[loc]

        logging.info("Finished calculating access requirements.")
        t3 = time.perf_counter()

        # Step Four: Use our requirements to build the path of items required for each player's go mode items.

        # Recursive function that replaces events in requirement sets with that event's own requirements.
        # This way we can later build a critical path expressed only in required items, with events being implied.
        def resolve_events(location, source, memo):
            if location in memo:
                return memo[location]

            assert location in source, \
                f"{location} is referenced as a requirement but step three never solved it"

            resolved_set = set()
            for item in source[location]:
                if item.is_event and item.location is not None:
                    resolved_set |= resolve_events(item.location, source, memo)
                else:
                    resolved_set.add(item)
            memo[location] = resolved_set
            return resolved_set

        resolved_reqs = {}
        for loc in location_reqs:
            resolve_events(loc, location_reqs, resolved_reqs)

        resolved_necessary = {}
        for loc in location_necessary:
            resolve_events(loc, location_necessary, resolved_necessary)


        # Next we determine which items/locations are worth hinting.
        item_counts = Counter((loc.item.name, loc.item.player) for loc in multiworld.get_filled_locations())

        hintable_classifications = frozenset(
            c | u for c in cls.hintable_item_classes
            for u in (ItemClassification(0), ItemClassification.useful)
        )
        def is_hintable(item):
            return (not item.is_event
                    and (item.classification in hintable_classifications)
                    and item_counts[(item.name, item.player)] <= cls.bulk_item_threshold
                    and item.name not in multiworld.worlds[item.player].hint_blacklist)

        hintable_reqs = {}
        bulk_reqs = {}
        for loc in resolved_reqs.keys():
            hintable_reqs[loc] = {i for i in resolved_reqs[loc] if is_hintable(i)}
            bulk_reqs[loc] = Counter(i.name for i in resolved_reqs[loc] if not is_hintable(i))

        # Determine a set of go-mode items for each player
        def go_mode_items(player):
            item_set = set()
            for loc in player_req_locs[player]:
                item = loc.item
                if item.player != player:
                    continue  # someone else's item
                if item.is_event:
                    if item.location is not None:
                        item_set |= resolved_reqs[item.location]  # findable items behind the event
                else:
                    item_set.add(item)
            return item_set

        go_mode_dict = {}
        for player in multiworld.player_ids:
            if multiworld.game[player] == cls.game: continue
            go_mode_dict[player] = go_mode_items(player)

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
            probe = CollectionState(multiworld)
            before = Counter(probe.prog_items[player])
            probe.collect(item, True)
            after = probe.prog_items[player]
            changed = [k for k in after if after[k] > before.get(k, 0)]
            return changed[0] if len(changed) == 1 else None

        def goal_deps(player, final_state):
            seeds = set()
            for item in goal_items(player, final_state):
                if item.is_event and item.location is not None:
                    seeds |= resolved_reqs[item.location]
                else:
                    seeds.add(item)
            return seeds

        def goal_items(player, final_state):
            lookup = items_by_name(player)  # name -> Item
            found = set()
            for name, item in lookup.items():
                key = name if name in final_state.prog_items[player] else stored_name(player, item)
                count = final_state.prog_items[player].get(key, 0) if key else 0
                if not count:
                    logging.warning("ArchiLogic: %s (P%d, %s) has no prog_items entry; "
                                    "excluded from goal dependencies.",
                                    name, player, multiworld.game[player])
                    continue
                probe = final_state.copy()
                probe.remove_item(key, player, count)
                probe.reachable_regions[player] = set()
                probe.blocked_connections[player] = set()
                probe.stale[player] = True
                if not multiworld.completion_condition[player](probe):
                    found.add(item)  # Item, not str
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
            """The items that guard the goal itself — the 'medallions' of this world.

            Three ways an item qualifies, checked in order:
              1. it's read by a goal location's own access rule
              2. the goal fails without it while every goal location stays reachable,
                 meaning the goal predicate reads it directly
              3. it guards the nearest guarded entrance into a goal location's region
            Anything else is upstream traversal or a cross-world cascade, not a gate.
            """
            glocs = goal_locations(player, final_state)

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
                if any(flips(gl, dep) for gl in glocs):
                    found.add(dep)
                # 2. goal reads it directly (glocs empty => goal has no location component)
                elif not glocs or all(gl.can_reach(probes[dep]) for gl in glocs):
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

            for gl in glocs:
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
                return deps
            return found


        paths: Dict[int, Dict[Item, Set[Item]]] = {}
        external: Dict[Item, int] = {}
        goal_dependencies = {}
        player_endpoints = {}
        for player in multiworld.player_ids:
            if multiworld.game[player] == cls.game: continue
            paths[player] = {}
            goal_dependencies[player] = goal_deps(player, state)
            stack = list(goal_dependencies[player])
            while stack:
                item = stack.pop()
                if item in paths[player]:
                    continue
                loc = item.location
                if loc is None:
                    paths[player][item] = set()
                elif loc.player != player:
                    paths[player][item] = set()
                    external[item] = loc.player
                else:
                    reqs = hintable_reqs[loc]
                    paths[player][item] = reqs
                    stack.extend(reqs)

            player_endpoints[player] = endpoint_items(player, state, goal_dependencies[player])

        logging.info("Completed calculating backing structures for hints.")
        t4 = time.perf_counter()
        logging.info("ArchiLogic timings: step1 %.2fs  step2 %.2fs  step3 %.2fs  step4 %.2fs  total %.2fs",
                     t1 - t0, t2 - t1, t3 - t2, t4 - t3, t4 - t0)
        #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        # Build up the logical backing for our hints:
        # WOTH and Path hints -> Determine critical path and each location on the path's requirements, and what's "hintable"
        # Region hints -> Determine amount of woth items in each region, give hints on woth and/or foolish regions
        # Who has my item? -> Maintain player links for locations. Like built in hints but less direct.
        #                  -> Can also hint region for item to point players in the right direction without "spoiling"
        #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
