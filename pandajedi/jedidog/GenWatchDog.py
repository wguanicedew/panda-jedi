import re
import sys

# logger
from pandacommon.pandalogger.PandaLogger import PandaLogger
from pandajedi.jedicore.MsgWrapper import MsgWrapper

from .TypicalWatchDogBase import TypicalWatchDogBase

logger = PandaLogger().getLogger(__name__.split(".")[-1])


# watchdog for general purpose
class GenWatchDog(TypicalWatchDogBase):
    # constructor
    def __init__(self, taskBufferIF, ddmIF):
        TypicalWatchDogBase.__init__(self, taskBufferIF, ddmIF)

    # main
    def doAction(self):
        # get logger
        tmpLog = MsgWrapper(logger)
        tmpLog.debug("start")
        # return
        tmpLog.debug("done")
        return self.SC_SUCCEEDED
