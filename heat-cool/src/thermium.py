
import asyncio
import json
import logging
import sys
from abc import ABC, abstractmethod
from argparse import ArgumentParser, Namespace
from contextlib import AsyncExitStack

from aiohttp import ClientSession
from nexia.const import BRAND_ASAIR
from nexia.home import NexiaHome
from nexia.thermostat import NexiaThermostat
from wakepy import keep

from util import Configure, PersistentData


class Thermium(object):
    """Controls thermostat activity"""

    @staticmethod
    def parseArgs() -> Namespace:
        """Parse command line arguments
        :return: A Namespace instance with parsed command line arguments
        """
        ap = ArgumentParser(description="Module to control thermostat activity",
                            epilog="Just displays status when no option is specified")
        group = ap.add_mutually_exclusive_group()
        group.add_argument("-e", "--enable", action="store_true",
                           help="enable auxiliary heat")
        group.add_argument("-r", "--restore", action="store_true",
                           help="restore auxiliary heat to previous state, disable by default")

        return ap.parse_args()
    # end parseArgs()

    async def main(self) -> None:
        """Primary entry point"""
        logging.debug(f"Starting {' '.join(sys.argv)}")

        async with AsyncExitStack() as cStack:
            # Prevent the computer from going to sleep until cStack closes
            if not cStack.enter_context(keep.running()).active:
                logging.info(f"Unable to prevent sleep using {keep.__name__}")

            # Register persistent data to save when cStack closes
            persistentData = cStack.enter_context(PersistentData())

            # Create ClientSession registered so it cleans up when cStack closes
            session = await cStack.enter_async_context(ClientSession())

            clArgs = self.parseArgs()
            processor: NexiaProc
            match True:
                case _ if clArgs.enable:
                    processor = AuxHeatEnabler(persistentData, session)
                case _ if clArgs.restore:
                    processor = AuxHeatRestorer(persistentData, session)
                case _:
                    processor = StatusPresenter(persistentData, session)
            # end match
            await processor.process()
        # end async with, callbacks are invoked in the reverse order of registration
    # end main()

# end class Thermium


class NexiaProc(ABC):
    """Abstract base class for Nexia users"""
    PRIOR_AUX_STATE = "priorAuxState"

    def __init__(self, persistentData: PersistentData, session: ClientSession):
        """Sole constructor
        :param persistentData: Persistent data reference
        :param session: Asynch client session to use
        """
        self.persistData = persistentData
        with open(Configure.findParmPath().joinpath("accesstoken.json"),
                  "r", encoding="utf-8") as accessFile:
            accessToken: dict[str, str] = json.load(accessFile)

        stateFile = Configure.findParmPath().joinpath(f"{BRAND_ASAIR}_config.persist")

        self.nexiaHome = NexiaHome(session, username=accessToken["username"],
                                   password=accessToken["password"],
                                   device_name="Python Script", brand=BRAND_ASAIR,
                                   state_file=stateFile)
        del accessToken
    # end __init__(ClientSession)

    @abstractmethod
    async def process(self) -> None:
        """Method that will accomplish the goal of this processor"""
        pass
    # end process()

    async def login(self) -> bool:
        await self.nexiaHome.login()
        await self.nexiaHome.update()

        return self.nexiaHome.thermostats is not None
    # end login()

    @staticmethod
    def auxOnOff(therm: NexiaThermostat) -> str:

        return "on" if therm.is_emergency_heat_active() else "off"
    # end auxOnOff(NexiaThermostat)

# end class NexiaProc


class AuxHeatEnabler(NexiaProc):
    """Processor to enable auxiliary heat"""

    async def process(self) -> None:
        if not await self.login():
            logging.error("Unable to contact thermostat")
            return

        for therm in self.nexiaHome.thermostats:
            auxHeatOn: bool = therm.is_emergency_heat_active()
            self.persistData.setVal(self.PRIOR_AUX_STATE, therm.get_device_id(), auxHeatOn)

            if auxHeatOn:
                logging.info(f"{therm.get_name()} auxiliary heat was already on")
            else:
                await therm.set_emergency_heat(True)
                logging.info(f"{therm.get_name()} auxiliary heat changed from"
                             f" off to {self.auxOnOff(therm)}")
        # end for each thermostat
    # end process()

# end class AuxHeatEnabler


class AuxHeatRestorer(NexiaProc):
    """Processor to restore auxiliary heat to previous state, defaulting to disabled"""

    async def process(self) -> None:
        if not await self.login():
            logging.error("Unable to contact thermostat")
            return

        for therm in self.nexiaHome.thermostats:
            auxHeatOn: bool = therm.is_emergency_heat_active()
            priorAuxHeat: bool | None = self.persistData.getVal(self.PRIOR_AUX_STATE,
                                                                therm.get_device_id())
            if priorAuxHeat is None:
                priorAuxHeat = False

            if auxHeatOn == priorAuxHeat:
                logging.info(f"{therm.get_name()} auxiliary heat was already {self.auxOnOff(therm)}")
            else:
                await therm.set_emergency_heat(priorAuxHeat)
                logging.info(f"{therm.get_name()} auxiliary heat changed from"
                             f" {"on" if auxHeatOn else "off"} to {self.auxOnOff(therm)}")
        # end for each thermostat
    # end process()

# end class AuxHeatRestorer


class StatusPresenter(NexiaProc):
    """Processor to display status"""

    async def process(self) -> None:
        if not await self.login():
            logging.error("Unable to contact thermostat")
            return

        for therm in self.nexiaHome.thermostats:
            logging.info(f"{therm.get_name()} auxiliary heat is {self.auxOnOff(therm)}")
    # end process()

# end class StatusPresenter


if __name__ == "__main__":
    Configure.logToFile()
    try:
        thermium = Thermium()
        asyncio.run(thermium.main())
    except Exception as xcpt:
        logging.error(xcpt)
        logging.debug(f"{xcpt.__class__.__name__} suppressed:", exc_info=xcpt)
