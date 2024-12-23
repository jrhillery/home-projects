
import asyncio
import json
import logging
import sys
from abc import ABC, abstractmethod
from argparse import ArgumentParser, Namespace
from contextlib import AsyncExitStack
from platform import node
from typing import NamedTuple
from urllib.parse import urlsplit, urlunsplit

import nexia.home
from aiohttp import ClientConnectorError, ClientError, ClientSession
from nexia.const import BRAND_ASAIR
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


class Sensor(NamedTuple):
    """Data object representing a sensor"""
    id: int
    name: str
    type: str
    serial_number: str
    weight: float
    temperature:int
    temperature_valid: bool
    humidity: int
    humidity_valid: bool
    has_online: bool
    has_battery: bool
# end class Sensor


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
        self.rootUrlSplits = urlsplit(self.nexiaHome.root_url)
        del accessToken
    # end __init__(PersistentData, ClientSession)

    @abstractmethod
    async def process(self) -> None:
        """Method that will accomplish the goal of this processor"""
        pass
    # end process()

    @staticmethod
    def get_sensors(therm: NexiaThermostat) -> list[Sensor]:
        """Get from the specified thermostat its sensor data objects
        :param therm: thermostat in question
        :return: list of sensor data objects
        """
        sensors_json = therm._get_thermostat_features_key("room_iq_sensors")["sensors"]
        sensors: list[Sensor] = []

        for sensor_json in sensors_json:
            sensors.append(Sensor(*[sensor_json[fld] for fld in Sensor._fields]))

        return sensors
    # end get_sensors(NexiaThermostat)

    @staticmethod
    def get_sensor_actions(therm: NexiaThermostat) -> dict[str, dict[str, str]]:
        """Get the actions offered by our sensors
        :param therm: thermostat in question
        :return: dictionary of sensor actions
        """
        return therm._get_thermostat_features_key("room_iq_sensors")["actions"]
    # end get_sensor_actions(NexiaThermostat)

    @staticmethod
    async def sensorData(therm: NexiaThermostat) -> str:
        """Create a text representation of select sensor details
        :param therm: thermostat in question
        :return: sensor detail text
        """
        sensorDetails: list[str] = [
            ("," if sensor.type == "thermostat" else f"{sensor.name}:")
            + f" {sensor.temperature}\u00B0"
              f" humidity {sensor.humidity}%"
            for sensor in NexiaProc.get_sensors(therm)]

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
        await self.nexiaHome.update()

        for therm in self.nexiaHome.thermostats:
            logging.debug(f"{therm.get_name()} at login{await self.sensorData(therm)}")

        return self.nexiaHome.thermostats is not None
    # end login()

    def resolveUrl(self, rawPath: str) -> str:
        """Determine the url of the specified raw path on the root host
        :param rawPath:
        :return: url resolved on the root host
        """
        rawPathParts = urlsplit(rawPath)

        return str(urlunsplit(rawPathParts._replace(
            scheme=self.rootUrlSplits.scheme, netloc=self.rootUrlSplits.netloc)))
    # end resolveUrl(str)

    async def loadCurrentSensorState(self, therm: NexiaThermostat) -> None:
        """Load into the specified thermostat the current state of its sensors
        :param therm: thermostat to load
        :return: None
        """
        actions = self.get_sensor_actions(therm)
        requestCurState = self.resolveUrl(actions["request_current_state"]["href"])

        async with await self.nexiaHome.post_url(requestCurState, {}) as response:
            pollingUrl = self.resolveUrl((await response.json())["result"]["polling_path"])
        retries = 50

        while retries:
            await asyncio.sleep(0.8)
            async with await self.nexiaHome._get_url(pollingUrl) as response:
                data = (await response.read()).strip()

            if data != b"null":
                status = json.loads(data)["status"]

                if status != "success":
                    logging.error(f"Unexpected status [{status}]"
                                  f" loading current sensor state")

                return
            retries -= 1
        # end while waiting for status

        logging.error("Gave up waiting for current sensor state")
    # end loadCurrentSensorState(NexiaThermostat)

    async def loadSensorStateRobustly(self, therm: NexiaThermostat) -> None:
        retries = 6

        while retries:
            try:
                await self.loadCurrentSensorState(therm)
                break
            except ClientError as e:
                logging.error(f"Load state retry needed due to {e.__class__.__name__}: {e}")
                await asyncio.sleep(15)
                retries -= 1
        # end while
    # end loadSensorStateRobustly(NexiaThermostat)

    @staticmethod
    def auxOnOff(therm: NexiaThermostat) -> str:
        """Return state of auxiliary heat
        :param therm: thermostat in question
        :return: on/off text for the auxiliary heat state of the specified thermostat
        """

        return "on" if therm.is_emergency_heat_active() else "off"
    # end auxOnOff(NexiaThermostat)

    async def refreshThermostatData(self, therm: NexiaThermostat) -> None:
        """Refresh thermostat data
        :param therm: thermostat to refresh
        :return: None
        """
        selfRef = self.resolveUrl(therm._get_thermostat_key("_links")["self"]["href"])

        async with await self.nexiaHome._get_url(selfRef) as response:
            therm.update_thermostat_json((await response.json())["result"])
    # end refreshThermostatData(NexiaThermostat)

# end class NexiaProc


class AuxHeatEnabler(NexiaProc):
    """Processor to enable auxiliary heat after persisting state"""

    async def process(self) -> None:
        if not await self.login():
            logging.error("Unable to contact thermostat")
            return

        for therm in self.nexiaHome.thermostats:
            auxHeatOn: bool = therm.is_emergency_heat_active()
            self.persistData.setVal(self.PRIOR_AUX_STATE, therm.get_device_id(), auxHeatOn)
            await self.loadSensorStateRobustly(therm)

            if auxHeatOn:
                await self.refreshThermostatData(therm)
                logging.info(f"{therm.get_name()} auxiliary heat was already on"
                             f"{await self.sensorData(therm)}")
            else:
                await therm.set_emergency_heat(True)
                logging.info(f"{therm.get_name()} auxiliary heat changed from"
                             f" off to {self.auxOnOff(therm)}{await self.sensorData(therm)}")
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
            await self.loadSensorStateRobustly(therm)

            if auxHeatOn == priorAuxHeat:
                await self.refreshThermostatData(therm)
                logging.info(f"{therm.get_name()} auxiliary heat was already"
                             f" {self.auxOnOff(therm)}{await self.sensorData(therm)}")
            else:
                await therm.set_emergency_heat(priorAuxHeat)
                logging.info(f"{therm.get_name()} auxiliary heat changed from"
                             f" {"on" if auxHeatOn else "off"} to {self.auxOnOff(therm)}"
                             f"{await self.sensorData(therm)}")
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
            await self.loadSensorStateRobustly(therm)
            await self.refreshThermostatData(therm)
            logging.info(f"{therm.get_name()} auxiliary heat is {self.auxOnOff(therm)}"
                         f"{await self.sensorData(therm)}")
    # end process()

# end class StatusPresenter


if __name__ == "__main__":
    Configure.logToFile()
    Configure.addRotatingFileHandler(nexia.home._LOGGER)
    try:
        thermium = Thermium()
        asyncio.run(thermium.main())
    except Exception as xcpt:
        logging.error(xcpt)
        logging.debug(f"{xcpt.__class__.__name__} suppressed:", exc_info=xcpt)
