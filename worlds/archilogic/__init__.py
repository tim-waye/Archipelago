from multiprocessing import Process

from worlds.LauncherComponents import Component, components, Type

from .ArchiLogicWorld import ArchiLogicWorld

def run_client():
    from worlds.archilogic.ArchiLogicClient import run_client
    p = Process(target=run_client)
    p.start()

components.append(Component("ArchiLogic Client", func=run_client, component_type=Type.CLIENT))
