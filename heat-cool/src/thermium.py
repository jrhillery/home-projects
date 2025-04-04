
import asyncio
import json
import logging
import sys
from abc import ABC, abstractmethod
from argparse import ArgumentParser, Namespace
from contextlib import AsyncExitStack
from platform import node

import nexia.home
from aiohttp import ClientConnectorError, ClientError, ClientSession
from nexia.const import BRAND_ASAIR
from nexia.thermostat import NexiaThermostat
from nexia.zone import NexiaThermostatZone
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
                           help="enable auxiliary heat after persisting state")
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
            # read username and password
            accessToken: dict[str, str] = json.load(accessFile)

        stateFile = Configure.findParmPath().joinpath(f"{BRAND_ASAIR}_config.persist")

        self.nexiaHome = nexia.home.NexiaHome(session, **accessToken, device_name=node(),
                                              brand=BRAND_ASAIR, state_file=stateFile)
        self.nexiaHome.log_response = False
        del accessToken
    # end __init__(PersistentData, ClientSession)

    @abstractmethod
    async def process(self) -> None:
        """Method that will accomplish the goal of this processor"""
        pass
    # end process()

    @staticmethod
    def sensorData(therm: NexiaThermostat) -> str:
        """Create a text representation of select sensor details
        :param therm: thermostat in question
        :return: sensor detail text
        """
        sensorDetails: list[str] = [
            (f"{sensor.name} {{}}," if sensor.type == "thermostat"
             else f"{sensor.name}:")
            + f" {sensor.temperature}\u00B0"
              f" humidity {sensor.humidity}%"
            for zone in therm.zones for sensor in zone.get_sensors()]

        return "; ".join(sensorDetails)
    # end sensorData(NexiaThermostat)

    async def login(self) -> bool:
        """Log in to the Nexia site
        :return: True when successfully logged-in with thermostats known
        """
        retries = 6

        while retries:
            try:
                await self.nexiaHome.login()
                break
            except ClientConnectorError as e:
                logging.error(f"Login retry needed due to {e.__class__.__name__}: {e}")
                await asyncio.sleep(15)
                retries -= 1
        # end while

        while retries:
            try:
                if await self.nexiaHome.update():
                    break
            except ClientError as e:
                logging.error(f"Update retry needed due to {e.__class__.__name__}: {e}")
            await asyncio.sleep(15)
            retries -= 1
        # end while

        for therm in self.nexiaHome.thermostats:
            logging.debug(self.sensorData(therm).format("at login"))

        return self.nexiaHome.thermostats is not None
    # end login()

    @staticmethod
    async def loadSensorStateRobustly(zone: NexiaThermostatZone) -> None:
        """Perform retries until the zone loads its current sensor state
        :param zone: zone to load
        """
        retries = 6

        while retries:
            try:
                if await zone.load_current_sensor_state(2, 20):
                    break
            except ClientError as e:
                logging.error(f"Load state retry needed due to {e.__class__.__name__}: {e}")
            await asyncio.sleep(15)
            retries -= 1
        # end while
    # end loadSensorStateRobustly(NexiaThermostatZone)

    async def loadCurrentSensorStates(self) -> None:
        """Load the current state of all zones' sensors in parallel"""
        async with asyncio.TaskGroup() as tg:
            for therm in self.nexiaHome.thermostats:
                for zone in therm.zones:
                    tg.create_task(self.loadSensorStateRobustly(zone))
        # end async with (tasks are awaited)
    # end loadCurrentSensorStates()

    @staticmethod
    def auxOnOff(therm: NexiaThermostat) -> str:
        """Return state of auxiliary heat
        :param therm: thermostat in question
        :return: on/off text for the auxiliary heat state of the specified thermostat
        """

        return "on" if therm.is_emergency_heat_active() else "off"
    # end auxOnOff(NexiaThermostat)

    async def changeAuxHeatIfNeeded(self, auxHeatOn: bool, auxHeatToSet: bool,
                                    therm: NexiaThermostat) -> None:
        """Change the auxiliary heat of a specified thermostat if needed. Also ensure data
        in the thermostat instance is refreshed and log a message with the new sensor data.
        :param auxHeatOn: existing auxiliary heat state - True for enabled, False for Disabled
        :param auxHeatToSet: desired auxiliary heat state - True for enabled, False for Disabled
        :param therm: thermostat in question
        :return:
        """
        auxHeatState = "on" if auxHeatOn else "off"

        if auxHeatOn == auxHeatToSet:
            await therm.refresh_thermostat_data()
            logging.info(self.sensorData(therm).format(
                f"auxiliary heat was already {auxHeatState}"))
        else:
            await therm.set_emergency_heat(auxHeatToSet)
            logging.info(self.sensorData(therm).format(
                f"auxiliary heat changed from {auxHeatState}"
                f" to {self.auxOnOff(therm)}"))
    # end changeAuxHeatIfNeeded(bool, bool, NexiaThermostat)

# end class NexiaProc


class AuxHeatEnabler(NexiaProc):
    """Processor to enable auxiliary heat after persisting state"""

    async def process(self) -> None:
        if not await self.login():
            logging.error("Unable to contact thermostat")
            return

        await self.loadCurrentSensorStates()

        for therm in self.nexiaHome.thermostats:
            auxHeatOn: bool = therm.is_emergency_heat_active()
            self.persistData.setVal(self.PRIOR_AUX_STATE, therm.get_device_id(), auxHeatOn)
            await self.changeAuxHeatIfNeeded(auxHeatOn, True, therm)
        # end for each thermostat
    # end process()

# end class AuxHeatEnabler


class AuxHeatRestorer(NexiaProc):
    """Processor to restore auxiliary heat to previous state, defaulting to disabled"""

    async def process(self) -> None:
        if not await self.login():
            logging.error("Unable to contact thermostat")
            return

        await self.loadCurrentSensorStates()

        for therm in self.nexiaHome.thermostats:
            auxHeatOn: bool = therm.is_emergency_heat_active()
            auxHeatToSet: bool = self.persistData.getVal(self.PRIOR_AUX_STATE,
                                                         therm.get_device_id(),
                                                         False)
            await self.changeAuxHeatIfNeeded(auxHeatOn, auxHeatToSet, therm)
        # end for each thermostat
    # end process()

# end class AuxHeatRestorer


class StatusPresenter(NexiaProc):
    """Processor to display status"""

    async def process(self) -> None:
        if not await self.login():
            logging.error("Unable to contact thermostat")
            return

        await self.loadCurrentSensorStates()

        for therm in self.nexiaHome.thermostats:
            await therm.refresh_thermostat_data()
            logging.info(self.sensorData(therm).format(
                f"auxiliary heat is {self.auxOnOff(therm)}"))
        # end for each thermostat
    # end process()

# end class StatusPresenter


if __name__ == "__main__":
    Configure.logToFile()
    Configure.addRotatingFileHandler(nexia.home._LOGGER,
                                     nexia.thermostat._LOGGER,
                                     nexia.zone._LOGGER)
    try:
        thermium = Thermium()
        asyncio.run(thermium.main())
    except Exception as xcpt:
        logging.error(xcpt)
        logging.debug(f"{xcpt.__class__.__name__} suppressed:", exc_info=xcpt)
