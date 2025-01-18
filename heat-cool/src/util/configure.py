
# noinspection PyPackageRequirements
import __main__ as main
import logging
import logging.config
import logging.handlers
from io import TextIOWrapper
from pathlib import Path


class LfRotatingFileHandler(logging.handlers.RotatingFileHandler):

    def _open(self) -> TextIOWrapper:
        logStream = super()._open()
        logStream.reconfigure(newline="\n")

        return logStream
    # end _open()

# end class LfRotatingFileHandler


class Configure(object):
    @staticmethod
    def logToFile() -> None:
        """Configure logging to file"""
        mainPath = Path(main.__file__)
        filePath = Path(mainPath.stem + ".log")

        if filePath.exists():
            # add a blank line each subsequent execution
            with open(filePath, "a", encoding="utf-8", newline="\n") as logFile:
                logFile.write("\n")

        logging.config.dictConfig({
            "version": 1,
            "formatters": {
                "detail": {
                    "format": "%(levelname)s %(asctime)s.%(msecs)03d %(module)s: %(message)s",
                    "datefmt": "%a %b %d %H:%M:%S"
                },
                "simple": {
                    "format": "%(asctime)s.%(msecs)03d: %(message)s",
                    "datefmt": "%a %H:%M:%S"
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "level": "INFO",
                    "formatter": "simple",
                    "stream": "ext://sys.stdout"
                },
                "file": {
                    "class": "util.configure.LfRotatingFileHandler",
                    "level": "DEBUG",
                    "formatter": "detail",
                    "filename": filePath,
                    "maxBytes": 120000,
                    "backupCount": 1,
                    "encoding": "utf-8"
                }
            },
            "root": {
                "level": "DEBUG",
                "handlers": ["console", "file"]
            }
        })
    # end logToFile()

    @staticmethod
    def addRotatingFileHandler(*loggers: logging.Logger) -> None:
        filePath = Path(loggers[0].name + ".log")

        if filePath.exists():
            # add a blank line each subsequent execution
            with open(filePath, "a", encoding="utf-8", newline="\n") as logFile:
                logFile.write("\n")

        rotatingFileHandler = LfRotatingFileHandler(
            filePath, maxBytes=120000, backupCount=1, encoding="utf-8")
        rotatingFileHandler.setLevel(logging.DEBUG)
        rotatingFileHandler.setFormatter(logging.Formatter(
            "%(levelname)s %(asctime)s.%(msecs)03d %(module)s: %(message)s",
            "%a %b %d %H:%M:%S"))

        for logger in loggers:
            logger.addHandler(rotatingFileHandler)
            logger.propagate = False
            logger.disabled = False
    # end addRotatingFileHandler(Logger)

    @staticmethod
    def findParmPath() -> Path:
        """Locate our parameter folder
        :return: A Path to our parameter folder
        """
        # look in child with a specific name
        pp = Path("parmFiles")

        if not pp.is_dir():
            # just use current directory
            pp = Path.cwd()

        return pp
    # end findParmPath()

# end class Configure
