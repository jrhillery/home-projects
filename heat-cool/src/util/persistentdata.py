
# noinspection PyPackageRequirements
import __main__ as main
import json
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Self

from util import Configure


class PersistentData(AbstractContextManager[Self]):

    def __init__(self):
        """Initialize this instance"""
        self.needsSave = False
    # end __init__()

    def __enter__(self) -> Self:
        """Allocate resources"""
        try:
            with open(self.persistPath(), "r", encoding="utf-8") as persistFile:
                self._data: dict[str, dict] = json.load(persistFile)
        except FileNotFoundError:
            self._data: dict[str, dict] = {}

        return self
    # end __enter__()

    def __exit__(self, exc_type, exc, exc_tb) -> None:
        """Close this instance and free up resources"""

        # Save this persistent data instance to file if needed
        if self.needsSave:
            with open(self.persistPath(), "w", encoding="utf-8", newline="\n") as persistFile:
                json.dump(self._data, persistFile, ensure_ascii=False, indent=2)
            self.needsSave = False
    # end __exit__(Type[BaseException] | None, BaseException | None, TracebackType | None)

    @staticmethod
    def persistPath() -> Path:
        """Locate our persist file
        :return: Path to our persist file
        """
        mainPath = Path(main.__file__)

        return Configure.findParmPath().joinpath(mainPath.stem + ".persist")
    # end persistPath()

    def setVal(self, category: str, instanceId: str, val: Any) -> None:
        """Store a value in this persistent data
        :param category: Classification given to this type of data
        :param instanceId: Identifier for this instance of data
        :param val: Value to store
        """
        if category not in self._data:
            self._data[category] = {instanceId: val}
            self.needsSave = True
        else:
            cat = self._data[category]

            if instanceId not in cat or cat[instanceId] != val:
                cat[instanceId] = val
                self.needsSave = True
    # end setVal(str, str, Any)

    def getVal(self, category: str, instanceId: str, default: Any=None) -> Any:
        """Retrieve a value from  this persistent data
        :param category: Classification given to this type of data
        :param instanceId: Identifier for this instance of data
        :param default: Value to use if no persisted data found
        :return: Persisted data value, if exists, otherwise default
        """
        try:
            return self._data[category][instanceId]
        except KeyError:
            return default
    # end getVal(str, str)

# end class PersistentData


if __name__ == "__main__":
    with PersistentData() as pd:
        bouncyToy: int | None = pd.getVal("bouncy", "j")

        if bouncyToy is None:
            bouncyToy = 646
            pd.setVal("bouncy", "j", bouncyToy)
            pd.setVal("clove", "s", 42)
        else:
            pd.setVal("bouncy", "s", 747)
            pd.setVal("bouncy", "j", 848)
