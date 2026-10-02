from ._profile import GameProfile, CustomEndpoint

class DonkeyKong64(GameProfile):
    game = "Donkey Kong 64"

    bulk_items = {"Golden Banana", "Banana Medal", "Banana Fairy", "Battle Crown"}

    display_names = {
        "Event, KRoolDefeated": "K. Rool Defeated",
        "Event, KRoolDonkey": "Donkey Phase",
        "Event, KRoolDiddy": "Diddy Phase",
        "Event, KRoolLanky": "Lanky Phase",
        "Event, KRoolTiny": "Tiny Phase",
        "Event, KRoolChunky": "Chunky Phase",
        "Event, KRoolDillo1": "Army 1 Phase",
        "Event, KRoolDog1": "Doga 1 Phase",
        "Event, KRoolJack": "Mad Jack Phase",
        "Event, KRoolPufftoss": "Pufftoss Phase",
        "Event, KRoolDog2": "Doga 2 Phase",
        "Event, KRoolDillo2": "Army 2 Phase",
        "Event, KRoolKKO": "Kutout Phase",
    }

    KROOL_PHASES = {
        "krool_donkey":   "Event, KRoolDonkey",
        "krool_diddy":    "Event, KRoolDiddy",
        "krool_lanky":    "Event, KRoolLanky",
        "krool_tiny":     "Event, KRoolTiny",
        "krool_chunky":   "Event, KRoolChunky",
        "krool_dillo1":   "Event, KRoolDillo1",
        "krool_dog1":     "Event, KRoolDog1",
        "krool_madjack":  "Event, KRoolJack",
        "krool_pufftoss": "Event, KRoolPufftoss",
        "krool_dog2":     "Event, KRoolDog2",
        "krool_dillo2":   "Event, KRoolDillo2",
        "krool_kutout":   "Event, KRoolKKO",
    }

    KONGS = {"Donkey", "Diddy", "Lanky", "Tiny", "Chunky"}

    def adjust_endpoints(self, endpoints, multiworld, player):
        new_endpoints = self._krool_phases(set(endpoints), multiworld, player)
        return new_endpoints | self._free_kongs(multiworld, player)

    def _krool_phases(self, endpoints, multiworld, player):
        """Remove the KRoolDefeated endpoint and replace it with the specific phases rolled for this seed"""
        krool_defeated = {end for end in endpoints if end.name == "Event, KRoolDefeated"}
        if not krool_defeated:
            return endpoints

        settings = multiworld.worlds[player].spoiler.settings
        phase_names = {name for opt, name in self.KROOL_PHASES.items() if getattr(settings, opt, False)}
        return (endpoints - krool_defeated) | self.items_from_names(multiworld, player, phase_names)

    def _free_kongs(self, multiworld, player):
        """Collect any Kongs that this seed doesn't start with"""
        starting_kongs = {item.name for item in multiworld.precollected_items[player]}
        locked_kongs = sorted(self.KONGS - starting_kongs)
        return {CustomEndpoint(name="Free Kongs", player=player, items=tuple(locked_kongs))} if locked_kongs else set()


